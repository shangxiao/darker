"""Darker - apply black reformatting to only areas edited since the last commit"""

import logging
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from difflib import unified_diff
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Set

from darker.black_diff import BlackArgs, run_black
from darker.chooser import choose_lines
from darker.command_line import ISORT_INSTRUCTION, parse_command_line
from darker.diff import (
    diff_and_get_opcodes,
    opcodes_to_chunks,
    opcodes_to_edit_linenums,
)
from darker.git import (
    git_diff_name_only,
    git_get_index_content,
    git_get_unmodified_content,
)
from darker.import_sorting import apply_isort, isort
from darker.utils import get_common_root, joinlines
from darker.verification import NotEquivalentError, verify_ast_unchanged

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _BaseGitFileAccessData:
    """Fields for Git file access classes (separate base to satisfy Mypy)"""

    paths: Set[Path]
    git_root: Path = field(init=False, repr=False)

    def __post_init__(self) -> None:
        super().__setattr__('git_root', get_common_root(self.paths))


class BaseGitFileAccess(_BaseGitFileAccessData, ABC):
    """Git information collector for given file paths"""

    @lru_cache()
    def _get_changed_files(self) -> Set[Path]:
        return git_diff_name_only(self.paths, self.git_root)

    def get_head_srcs(self) -> Dict[Path, List[str]]:
        """Return content of the files as it was at Git HEAD"""
        return {
            src: git_get_unmodified_content(src, self.git_root)
            for src in self._get_changed_files()
        }

    def get_absolute_path(self, relative_path: Path) -> Path:
        """Convert a relative path to a file in the repository to an absolute one"""
        return self.git_root / relative_path

    @abstractmethod
    def get_modified_srcs(self) -> Dict[Path, str]:
        """Must be overridden to return modified text of the files"""

    @abstractmethod
    def write(self, relative_path: Path, content: str) -> None:
        """Must be overridden to overwrite the given file with the given content"""


class WorkTreeFileAccess(BaseGitFileAccess):
    """Collect information about the Git working tree"""

    def get_modified_srcs(self) -> Dict[Path, str]:
        """Return current content of the files in the Git working tree"""
        return {
            src: (self.git_root / src).read_text() for src in self._get_changed_files()
        }

    def write(self, relative_path: Path, content: str) -> None:
        """Overwrite the given file with the given content"""
        self.get_absolute_path(relative_path).write_text(content)


class IndexFileAccess(BaseGitFileAccess):
    """Collect information about the Git staging index"""

    def get_modified_srcs(self) -> Dict[Path, str]:
        """Return current content of the files in the Git staging index"""
        return {
            src: joinlines(git_get_index_content(src, self.git_root))
            for src in self._get_changed_files()
        }

    def write(self, relative_path: Path, content: str) -> None:
        """Overwrite the given file with the given content"""
        self.get_absolute_path(relative_path).write_text(content)


def format_edited_parts(
    enable_isort: bool,
    black_args: BlackArgs,
    print_diff: bool,
    git_file_access: BaseGitFileAccess,
) -> None:
    """Black (and optional isort) formatting for chunks with edits since the last commit

    1. run isort on each edited file
    2. diff HEAD and worktree for all file & dir paths on the command line
    3. extract line numbers in each edited to-file for changed lines
    4. run black on the contents of each edited to-file
    5. get a diff between the edited to-file and the reformatted content
    6. convert the diff into chunks, keeping original and reformatted content for each
       chunk
    7. choose reformatted content for each chunk if there were any changed lines inside
       the chunk in the edited to-file, or choose the chunk's original contents if no
       edits were done in that chunk
    8. concatenate all chosen chunks
    9. verify that the resulting reformatted source code parses to an identical AST as
       the original edited to-file
    10. write the reformatted source back to the original file

    :param enable_isort: ``True`` to also run ``isort`` first on each changed file
    :param black_args: Command-line arguments to send to ``black.FileMode``
    :param print_diff: ``True`` to output diffs instead of modifying source files
    :param git_file_access: An object through which files in the Git repository are read
                            from and written to.

    """
    head_srcs = git_file_access.get_head_srcs()
    git_modified_srcs = git_file_access.get_modified_srcs()

    # 1. run isort
    if enable_isort:
        config = black_args.get("config")
        line_length = black_args.get("line_length")
        edited_srcs = {
            src: apply_isort(edited_content, src, config, line_length)
            for src, edited_content in git_modified_srcs.items()
        }
    else:
        edited_srcs = git_modified_srcs

    for src_relative, edited_content in edited_srcs.items():
        max_context_lines = len(edited_content)
        for context_lines in range(max_context_lines + 1):
            src = git_file_access.get_absolute_path(src_relative)
            edited = edited_content.splitlines()
            head_lines = head_srcs[src_relative]

            # 2. diff HEAD and worktree for the file
            edited_opcodes = diff_and_get_opcodes(head_lines, edited)

            # 3. extract line numbers in each edited to-file for changed lines
            edited_linenums = list(
                opcodes_to_edit_linenums(edited_opcodes, context_lines)
            )
            if (
                enable_isort
                and not edited_linenums
                and edited_content == git_modified_srcs[src_relative]
            ):
                logger.debug("No changes in %s after isort", src)
                break

            # 4. run black
            formatted = run_black(src, edited_content, black_args)
            logger.debug("Read %s lines from edited file %s", len(edited), src)
            logger.debug("Black reformat resulted in %s lines", len(formatted))

            # 5. get the diff between each edited and reformatted file
            opcodes = diff_and_get_opcodes(edited, formatted)

            # 6. convert the diff into chunks
            black_chunks = list(opcodes_to_chunks(opcodes, edited, formatted))

            # 7. choose reformatted content
            chosen_lines: List[str] = list(choose_lines(black_chunks, edited_linenums))

            # 8. concatenate chosen chunks
            result_str = joinlines(chosen_lines)

            # 9. verify
            logger.debug(
                "Verifying that the %s original edited lines and %s reformatted lines "
                "parse into an identical abstract syntax tree",
                len(edited),
                len(chosen_lines),
            )
            try:
                verify_ast_unchanged(
                    edited_content, result_str, black_chunks, edited_linenums
                )
            except NotEquivalentError:
                # Diff produced misaligned chunks which couldn't be reconstructed into
                # a partially re-formatted Python file which produces an identical AST.
                # Try again with a larger `-U<context_lines>` option for `git diff`,
                # or give up if `context_lines` is already very large.
                if context_lines == max_context_lines:
                    raise
                logger.debug(
                    "AST verification failed. "
                    "Trying again with %s lines of context for `git diff -U`",
                    context_lines + 1,
                )
                continue
            else:
                # 10. A re-formatted Python file which produces an identical AST was
                #     created successfully - write an updated file
                #     or print the diff
                if print_diff:
                    difflines = list(
                        unified_diff(
                            git_modified_srcs[src_relative].splitlines(),
                            chosen_lines,
                            src.as_posix(),
                            src.as_posix(),
                        )
                    )
                    if len(difflines) > 2:
                        h1, h2, *rest = difflines
                        print(h1, end="")
                        print(h2, end="")
                        print("\n".join(rest))
                else:
                    logger.info("Writing %s bytes into %s", len(result_str), src)
                    git_file_access.write(src_relative, result_str)
                break


def main(argv: List[str] = None) -> None:
    """Parse the command line and apply black formatting for each source file"""
    if argv is None:
        argv = sys.argv[1:]
    args = parse_command_line(argv)
    log_level = logging.WARNING - sum(args.log_level or ())
    logging.basicConfig(level=log_level)
    if log_level == logging.INFO:
        formatter = logging.Formatter("%(levelname)s: %(message)s")
        logging.getLogger().handlers[0].setFormatter(formatter)

    # Make sure we don't get excessive debug log output from Black
    logging.getLogger("blib2to3.pgen2.driver").setLevel(logging.WARNING)

    if args.isort and not isort:
        logger.error(f"{ISORT_INSTRUCTION} to use the `--isort` option.")
        exit(1)

    black_args = BlackArgs()
    if args.config:
        black_args["config"] = args.config
    if args.line_length:
        black_args["line_length"] = args.line_length
    if args.skip_string_normalization:
        black_args["skip_string_normalization"] = args.skip_string_normalization

    paths = {Path(p) for p in args.src}
    format_edited_parts(args.isort, black_args, args.diff, WorkTreeFileAccess(paths))
    if args.pre_commit:
        format_edited_parts(args.isort, black_args, args.diff, IndexFileAccess(paths))


if __name__ == "__main__":
    main()

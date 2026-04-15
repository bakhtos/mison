import os
from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass
from json import JSONEncoder, JSONDecoder
from typing import List, final
from collections import deque
import subprocess

from git import Repo, NULL_TREE
from pydriller import Repository
import requests

__all__ = ['github_mine_commits', 'Commit', 'ModifiedFile',
           'CommitJSONEncoder', 'CommitJSONDecoder', 'CommitMiner', 'GitMiner',
           'PydrillerMiner']


class ModificationType(Enum):
    ADD = 1
    COPY = 2
    RENAME = 3
    DELETE = 4
    MODIFY = 5
    UNKNOWN = 6


@dataclass
class ModifiedFile:
    new_path: str
    old_path: str
    modification_type: ModificationType
    additions: int = 0
    deletions: int = 0
    path: str = None

    def __post_init__(self):
        if self.path is None:
            if self.modification_type == ModificationType.DELETE:
                self.path = self.old_path
            else:
                self.path = self.new_path
        if self.modification_type != ModificationType.RENAME:
            self.old_path = None
            self.new_path = None

@dataclass
class Commit:
    sha: str
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str
    commit_date: datetime
    modified_files: List[ModifiedFile]

    def __hash__(self):
        return hash(self.sha)


class CommitJSONEncoder(JSONEncoder):
    def default(self, o):
        if isinstance(o, Commit):
            d = {k: v for k, v in o.__dict__.items() if k != "modified_files"}
            d["modified_files"] = [obj.__dict__ for obj in o.__dict__["modified_files"]]
            return d
        elif isinstance(o, datetime):
            return o.isoformat()
        elif isinstance(o, ModificationType):
            match o:
                case ModificationType.ADD:
                    return "add"
                case ModificationType.DELETE:
                    return "delete"
                case ModificationType.RENAME:
                    return "rename"
                case ModificationType.COPY:
                    return "copy"
                case ModificationType.MODIFY:
                    return "modify"
                case ModificationType.UNKNOWN:
                    return "unknown"
        elif isinstance(o, set):
            return list(o)
        else:
            return super().default(o)


class CommitJSONDecoder(JSONDecoder):
    def __init__(self, *args, **kwargs):
        super().__init__(object_hook=self.object_hook, *args, **kwargs)

    def object_hook(self, obj):
        if isinstance(obj, dict):
            if "new_path" in obj:
                match obj["modification_type"]:
                    case "add":
                        obj["modification_type"] = ModificationType.ADD
                    case "delete":
                        obj["modification_type"] = ModificationType.DELETE
                    case "copy":
                        obj["modification_type"] = ModificationType.COPY
                    case "rename":
                        obj["modification_type"] = ModificationType.RENAME
                    case "modify":
                        obj["modification_type"] = ModificationType.MODIFY
                    case "unknown":
                        obj["modification_type"] = ModificationType.UNKNOWN
                return ModifiedFile(**obj)
            elif "sha" in obj and "author_email" in obj:
                obj["commit_date"] = datetime.fromisoformat(obj["commit_date"])
                modified_files = [self.object_hook(item) for item in obj["modified_files"]]
                obj["modified_files"] = modified_files
                return Commit(**obj)
            else:
                return {key: self.object_hook(value) for key, value in obj.items()}
        elif isinstance(obj, list):
            return [self.object_hook(item) for item in obj]
        return obj


class CommitMiner(ABC):
    """ABC for mining commits from different repositories with different VCS."""
    def __init__(self, repo: str):
        """
        :param repo: String indicating the repository
        """
        self._repo = repo

    @property
    @final
    def repo(self):
        """String indicating the repository to be mined. Either a path or URL"""
        return self._repo

    @abstractmethod
    def mine_commits(self) -> List[Commit]:
        """Mine the repository using the specified parameters and return a list of Commits"""
        ...


class GitMiner(CommitMiner):
    """Commit miner using the 'git' library as backend"""

    def __init__(self, repo: str, start_commit: str = None,
                         skip_merge_commits=False):
        """Mine all commits with a BFS from a given commit to its parents.

        :param repo: str, Path to repository to be mined
        :param start_commit: str, Hash of the starting commit
        :param skip_merge_commits: bool, Skip commits with more than 1 parent
        """
        super().__init__(repo)
        self._repo_obj = Repo(repo)
        self._skip_merge_commits = skip_merge_commits
        self._start_commit = start_commit

    def mine_commits(self) -> List[Commit]:
        commits: List[Commit] = []
        visited = set()
        if self._start_commit is None:
            start_commit = self._repo_obj.head.commit
        else:
            start_commit = self._repo_obj.commit(self._start_commit)
        queue = deque([start_commit])

        while queue:
            commit = queue.popleft()
            print(f"Processing {commit.hexsha}")
            if commit.hexsha in visited: continue
            visited.add(commit.hexsha)

            # Enqueue parent commits
            for parent in commit.parents:
                queue.append(parent)

            # Skip merge commits (more than one parent)
            if self._skip_merge_commits and len(commit.parents) > 1: continue

            modified_files: List[ModifiedFile] = []

            # --- ✅ get file stats directly from Git (binary-safe)
            result = subprocess.run(
                ["git", "-C", self._repo, "show", "--numstat", "--format=", commit.hexsha],
                capture_output=True, text=True, check=True
            )
            numstat = {}
            for line in result.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) != 3:
                    continue
                added, deleted, path = parts
                if added == "-" or deleted == "-":  # binary file
                    numstat[path] = (0, 0)
                else:
                    numstat[path] = (int(added), int(deleted))
            # ---

            # Diff against parent or NULL_TREE for change types
            if commit.parents:
                parent = commit.parents[0]
                diffs = parent.diff(commit, create_patch=False)
            else:
                diffs = commit.diff(NULL_TREE, create_patch=False)

            for diff in diffs:
                # default additions/deletions = 0; override with numstat if available
                additions, deletions = numstat.get(diff.b_path or diff.a_path, (0, 0))

                if diff.renamed:
                    mod_type = ModificationType.RENAME
                elif diff.new_file:
                    mod_type = ModificationType.ADD
                elif diff.deleted_file:
                    mod_type = ModificationType.DELETE
                else:
                    mod_type = ModificationType.MODIFY

                modified_files.append(
                    ModifiedFile(
                        new_path=diff.b_path,
                        old_path=diff.a_path,
                        modification_type=mod_type,
                        additions=additions,
                        deletions=deletions,
                    )
                )

            commits.append(
                Commit(
                    sha=commit.hexsha,
                    author_name=commit.author.name,
                    author_email=commit.author.email,
                    committer_name=commit.committer.name,
                    committer_email=commit.committer.email,
                    commit_date=commit.committed_datetime,
                    modified_files=modified_files,
                )
            )

        return commits


class PydrillerMiner(CommitMiner):
    """Commit miner using the Pydriller library as backend"""

    def __init__(self, repo: str, **kwargs):
        """Mining git repository commits and file modifications with Pydriller library

        :param repo: str, Path to the repository (can be online, will be temporarily cloned)
        :param kwargs: kwargs for pydriller.Repository (filters, commits range)
        """

        super().__init__(repo)
        self._pydriller_kwargs = {k: v for k, v in kwargs.items() if v is not None}

    def mine_commits(self) -> List[Commit]:
        data = []

        for commit in Repository(self._repo, **self._pydriller_kwargs).traverse_commits():
            modified_files = []
            print(f"Processing {commit.hash}")
            for file in commit.modified_files:
                new_path = None if file.new_path is None else f"{commit.project_name}/{Path(file.new_path).as_posix()}"
                old_path = None if file.old_path is None else f"{commit.project_name}/{Path(file.old_path).as_posix()}"
                modified_files.append(ModifiedFile(new_path=new_path, old_path=old_path,
                                                   modification_type=file.change_type, deletions=file.deleted_lines,
                                                   additions=file.added_lines))
            data.append(Commit(sha=commit.hash, author_name=commit.author.name, author_email=commit.author.email.lower(),
                               committer_name=commit.committer.name, committer_email=commit.committer.email.lower(),
                               commit_date=commit.committer_date, modified_files=modified_files))

        return data


def github_mine_commits(repo: str, github_token=None, per_page=100) -> List[Commit]:
    """
    Mining git repository commits and file modifications with GitHub API.
    :param repo: str, address of the repository on GitHub
    :param github_token: str, the GitHub API token to use for API access; if None, will try to get GITHUB_TOKEN env
    :param per_page: (optional) amount of commits to return per page, passed to the GitHub API request
    :return: pandas DataFrame with all mined commits and file modifications
    :raise ValueError: if the GitHub API is not provided neither as parameter not environment variable
    """

    if github_token is None:
        github_token = os.getenv('GITHUB_TOKEN')
        if github_token is None:
            raise ValueError("GitHub token needs to be provided either as a function/cli argument or in env. var. GITHUB_TOKEN")

    repo = repo.removeprefix('https://github.com/')
    owner, repo = repo.split("/")
    project_commits_query = f"https://api.github.com/repos/{owner}/{repo}/commits"
    headers = {'Authorization': f'token {github_token}'}
    params = {'per_page': per_page}

    commits_data = []
    page = 1

    while True:
        params['page'] = page
        response = requests.get(project_commits_query, headers=headers, params=params)
        project_commits_data: list[dict] = response.json()

        if not project_commits_data:
            break

        for item in project_commits_data:
            commit_sha = item['sha']
            print(f"Processing {commit_sha}")
            author_name: str = item.get('commit', {}).get('author', {}).get('name', None)
            author_email: str = item.get('commit', {}).get('author', {}).get('email', None)
            committer_name: str = item.get('commit', {}).get('committer', {}).get('name', None)
            committer_email: str = item.get('commit', {}).get('committer', {}).get('email', None)
            commit_date: str = item.get('commit', {}).get('committer', {}).get('date', None)

            if commit_date:
                commit_date: datetime = datetime.fromisoformat(commit_date.replace("Z", "+00:00"))

            # Fetch detailed commit changes
            commit_changes_query = f"{project_commits_query}/{commit_sha}"
            commit_changes_response = requests.get(commit_changes_query, headers=headers)
            commit_changes_data = commit_changes_response.json()

            modified_files = []
            for file in commit_changes_data.get("files", []):
                status = file.get("status")
                match status:
                    case "added":
                        status = ModificationType.ADD
                    case "removed":
                        status = ModificationType.DELETE
                        # Compatibility with PyDriller
                        file["previous_filename"] = file.get("filename")
                        file["filename"] = None
                    case "modified":
                        status = ModificationType.MODIFY
                    case "renamed":
                        status = ModificationType.RENAME
                    case "copied":
                        status = ModificationType.COPY
                    case _:
                        status = ModificationType.UNKNOWN

                new_path = f"{repo}/{file.get('filename')}"
                old_path = None if file.get("previous_filename", None) is None else f"{repo}/{file.get('previous_filename')}"
                modified_files.append(
                    ModifiedFile(new_path=new_path, old_path=old_path,
                                 modification_type=status, additions=file.get("additions", 0),
                                 deletions=file.get("deletions", 0)))
            commit_entry = Commit(
                sha=commit_sha,
                author_name=author_name,
                author_email=author_email,
                committer_name=committer_name,
                committer_email=committer_email,
                commit_date=commit_date,
                modified_files=modified_files)
            commits_data.append(commit_entry)

        page += 1

    return commits_data

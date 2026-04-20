from mison.miner import Commit

from collections.abc import Mapping
from typing import Union, Callable, Iterable, Set, Optional

import networkx as nx
from pydriller import ModificationType

__all__ = ['DevComponentMapping']


class Node(str):
    pass


class Developer(Node):
    pass


class Component(Node):
    pass


class File(Component):
    pass


class DevComponentMapping:
    def __init__(self, commits: Iterable[Commit], map_renamed_files=False):
        """
        Construct a mapping of developers committing to files.

        This function generates a NetworkX graph (`DevFileMapping`) that represents the relationship between
        developers and the files they have modified. The resulting graph consists of two types of nodes:

        - **Developers** (`type="dev"`)
        - **Files** (`type="file"`)

        Edges between developers and files indicate that a developer has modified a particular file in at least
        one commit. Each edge includes a `"commits"` attribute, which is a list of `mison.miner.Commit` objects
        representing the commits where the developer changed the file.

        ### Graph Properties:
        - **Nodes**: Each node has a `"type"` attribute set to either `"dev"` (developer) or `"file"` (file).
        - **Edges**: An edge exists between a developer and a file if the developer has modified that file.
          The `"commits"` attribute on the edge contains the list of related commits.

        :param commits: An Iterable, list of positional argument or a single instance of mison.miner.Commit objects
        """
        self._G: nx.Graph[Node] = nx.Graph()
        self._devs: Set[Developer] = set()
        self._components: Set[Component] = set()

        for commit in commits:
            dev = Developer(commit.author_email)
            self._G.add_node(dev, type='dev', aliases=set())
            self._devs.add(dev)
            for file in commit.modified_files:
                file = File(file.path)
                self._G.add_node(file, type='component', other_names = set())
                self._components.add(file)
                if self._G.has_edge(dev, file):
                    self._G[dev][file]['commits'] += [commit]
                else:
                    self._G.add_edge(dev, file, commits=[commit])

        if map_renamed_files:
            self._map_renamed_files()

    @property
    def devs(self):
        return frozenset(self._devs)

    @property
    def components(self):
        return frozenset(self._components)


    def _map_renamed_files(self):
        """
        Map renamed files in a `DevFileMapping` graph to their latest filenames.

        This function updates a `DevFileMapping` graph by tracking file renames across commits and
        ensuring that all references to old filenames are mapped to their latest version.

        ### How It Works:
        - Resolve each file's latest name by scanning the commit history for file renames
            (`ModificationType.RENAME`) and records rename chains.
        - Updates the graph:
          - If an old filename has a newer version, it is **replaced** with the latest filename.
          - Merge edges from the old file node to the new file node while preserving commit data.
          - Store the list of old filenames under the `"old_paths"` attribute of the latest filename.

        ### Graph Updates:
        - **Nodes**: Old filenames are removed, and their data is merged into the latest filename node.
        - **Edges**: Developers previously linked to an old filename are reconnected to the latest filename.
        - **Attributes**: Each latest filename node retains a list of `"old_paths"` for traceability.

        :param self: A `DevFileMapping` graph
        :return: An updated `DevFileMapping` graph where all renamed files are mapped to their newest filenames.
        """
        commits = set()
        for _, _, edge_commits in self._G.edges.data(data="commits"):
            commits.update(edge_commits)
        commits = sorted(commits, key=lambda x: x.commit_date)

        chains = []
        tip_to_chain = {}

        for commit in commits:
            for modified_file in commit.modified_files:
                if modified_file.modification_type != ModificationType.RENAME:
                    continue

                old = File(modified_file.old_path)
                new = File(modified_file.new_path)

                if old in tip_to_chain:
                    chain = tip_to_chain.pop(old)
                    chain["files"].append(new)
                    chain["latest_time"] = commit.commit_date
                    tip_to_chain[new] = chain
                else:
                    chain = {
                        "files": [old, new],
                        "latest_time": commit.commit_date,
                    }
                    chains.append(chain)
                    tip_to_chain[new] = chain

        chains.sort(key=lambda chain: chain["latest_time"])

        for chain in chains:
            newest_file = chain["files"][-1]
            seen_in_chain = set()

            for old_file in reversed(chain["files"][:-1]):
                if old_file == newest_file or old_file in seen_in_chain:
                    continue
                seen_in_chain.add(old_file)

                old_names = {old_file}
                old_names.update(self._G.nodes[old_file]["other_names"])

                self._switch_node(old_file, newest_file)
                self._G.nodes[newest_file]["other_names"].update(old_names)

    def _switch_node(self, old: Union[Developer, Component], new: Union[Developer, Component]) -> None:
        if not (
                (isinstance(old, Developer) and isinstance(new, Developer))
                or
                (isinstance(old, Component) and isinstance(new, Component))
        ):
            raise ValueError(
                f"Old and new must be either both Developer or both Component, "
                f"received {type(old)} and {type(new)}"
            )

        if old == new:
            return

        if old not in self._G:
            raise KeyError(f"{old!r} is not in the graph")

        if new not in self._G:
            self.add_node(new)

        # Rewire all edges old--neighbor to new--neighbor.
        # If new--neighbor already exists, merge commits.
        for neighbor, edge_data in list(self._G[old].items()):
            if self._G.has_edge(new, neighbor):
                old_commits = edge_data["commits"]
                self._G[new][neighbor]["commits"] = (
                        old_commits + self._G[new][neighbor]["commits"]
                )
            else:
                self._G.add_edge(new, neighbor, **edge_data)

        self.remove_node(old)

    def add_node(self, node: Node):
        if isinstance(node, Developer):
            self._devs.add(node)
            self._G.add_node(node, type="dev", aliases=set())
        elif isinstance(node, File):
            self._components.add(node)
            self._G.add_node(node, type="component", other_names = set())
        elif isinstance(node, Component):
            self._components.add(node)
            self._G.add_node(node, type="component", files=set())
        else:
            raise TypeError(
                f"Node {node} must be of type Node (Developer or Component), received {type(node)}"
            )

    def remove_node(self, node: Node):
        if isinstance(node, Developer):
            self._devs.discard(node)
        elif isinstance(node, Component):
            self._components.discard(node)
        else:
            raise TypeError(
                f"Node {node} must be of type Node (Developer or Component), received {type(node)}"
            )
        self._G.remove_node(node)


    def map_developers(
            self,
            developer_mapping: Union[Mapping[Developer, Developer],
            Callable[[Developer], Developer]]):
        """
        Remap developers in a DevFileMapping.

        This function updates the DevFileMapping by replacing developers
        according to the provided `developer_mapping`. Each occurrence of an old developer (`old_dev`)
        is replaced with a new developer (`new_dev = developer_mapping[old_dev]`), while preserving and
        reconnecting the links accordingly.

        If multiple developers are mapped to the same new developer, their links to components
        are merged under the new developer.

        ### Developer Mapping Options:
        - **Dictionary (`dict[old_dev, new_dev]`)**: Maps specific developers to new ones. Developers not included
          in the dictionary remain unchanged.
        - **Function (`Callable[[old_dev], new_dev]`)**: A function that takes a developer email and returns the
          new email. If the function returns the same developer email, the developer remains unchanged.

        :param developer_mapping: A dictionary or function mapping old developers to new ones.
        """
        old_devs = list(self._devs)
        if callable(developer_mapping):
            mapping_iter = map(developer_mapping, old_devs)
        elif isinstance(developer_mapping, Mapping):
            mapping_iter = map(lambda x: developer_mapping.get(x, x), old_devs)
        else:
            raise TypeError("developer_mapping must be a Mapping or a Callable")
        for old_dev, new_dev in zip(old_devs, mapping_iter):
            if old_dev == new_dev:
                print(f"Keeping {old_dev}")
                continue
            print(f"Replacing {old_dev} with {new_dev}")
            old_names = {old_dev}
            old_names.update(self._G.nodes[old_dev]["aliases"])
            self._switch_node(old_dev, new_dev)
            self._G.nodes[new_dev]["aliases"].update(old_names)

    def map_files_to_components(
            self,
            component_mapping: Union[
                Mapping[File, Optional[Component]],
                Callable[[File], Optional[Component]]]):
        """
        Construct a `DevComponentMapping` graph from a `DevFileMapping` graph by grouping files into components.

        This function assignes files to components using the provided `component_mapping`.
        Each file in `DevFileMapping` is mapped to a component using `component_mapping(file)`.
        Developers will then be linked to components instead of individual files.

        ### Component Mapping Options:
        - **Dictionary (`dict[File, Component]`)** mapping files to their corresponding components.
        - **Function (`Callable[[File], Component]`)** returning the component for a given file.
        If a file is **not present in the dictionary** or if the function **returns `None`**, the file is
        **excluded**, and commits involving that file are omitted.

        :param component_mapping: A dictionary or function that maps files to components.
        """
        old_files = [file for file in self._components if isinstance(file, File)]
        if callable(component_mapping):
            mapping_iter = map(component_mapping, old_files)
        elif isinstance(component_mapping, Mapping):
            mapping_iter = map(lambda x: component_mapping.get(x, None), old_files)
        else:
            raise TypeError("component_mapping must be a Mapping or a Callable")
        for file, component in zip(old_files, mapping_iter):
            if component is None:
                print(f"File {file} does not belong to a component")
                self.remove_node(file)
                continue
            print(f"File {file} belongs to {component}")
            old_names = {file}
            old_names.update(self._G.nodes[file]["other_names"])
            self._switch_node(file, component)
            self._G.nodes[component]["files"].update(old_names)

    def map_components(
            self,
            component_mapping: Union[
                Mapping[Component, Component],
                Callable[[Component], Component]]):
        """
        Remap non-file components in a `DevComponentMapping`.

        This function updates the mapping by replacing non-file components
        according to the provided `component_mapping`. Each occurrence of an old
        component (`old_component`) is replaced with a new component
        (`new_component = component_mapping[old_component]`), while preserving and
        reconnecting the links accordingly.

        If multiple components are mapped to the same new component, their links
        to developers are merged under the new component, and their `"files"`
        node attributes are merged into the new component's `"files"` set.

        ### Component Mapping Options:
        - **Dictionary (`dict[old_component, new_component]`)**: Maps specific
          components to new ones. Components not included in the dictionary remain
          unchanged.
        - **Function (`Callable[[old_component], new_component]`)**: A function
          that takes a component and returns the new component. If the function
          returns the same component, the component remains unchanged.

        :param component_mapping: A dictionary or function mapping old components
            to new ones.
        """
        old_components = list(self._components)
        if callable(component_mapping):
            mapping_iter = map(component_mapping, old_components)
        elif isinstance(component_mapping, Mapping):
            mapping_iter = map(lambda x: component_mapping.get(x, x), old_components)
        else:
            raise TypeError("component_mapping must be a Mapping or a Callable")
        for old_component, new_component in zip(old_components, mapping_iter):
            if old_component == new_component:
                print(f"Keeping {old_component}")
                continue
            print(f"Replacing {old_component} with {new_component}")
            old_files = self._G.nodes[old_component]["files"]
            self._switch_node(old_component, new_component)
            self._G.nodes[new_component]["files"].update(old_files)

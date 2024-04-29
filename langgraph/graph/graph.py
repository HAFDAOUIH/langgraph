import logging
from collections import Counter, defaultdict
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    NamedTuple,
    Optional,
    Sequence,
    Union,
    cast,
)

from langchain_core.runnables import Runnable
from langchain_core.runnables.base import RunnableLike
from langchain_core.runnables.config import RunnableConfig
from langchain_core.runnables.graph import Node as RunnableGraphNode

from langgraph.channels.ephemeral_value import EphemeralValue
from langgraph.checkpoint import BaseCheckpointSaver
from langgraph.constants import TAG_HIDDEN
from langgraph.pregel import Channel, Pregel
from langgraph.pregel.read import PregelNode
from langgraph.pregel.write import ChannelWrite, ChannelWriteEntry
from langgraph.utils import DrawableGraph, RunnableCallable, coerce_to_runnable

logger = logging.getLogger(__name__)

START = "__start__"
END = "__end__"


class Branch(NamedTuple):
    path: Runnable[Any, Union[str, list[str]]]
    ends: Optional[dict[str, str]]
    then: Optional[str] = None

    def run(
        self,
        writer: Callable[[list[str]], Optional[Runnable]],
        reader: Optional[Callable[[RunnableConfig], Any]] = None,
    ) -> None:
        return ChannelWrite.register_writer(
            RunnableCallable(
                func=self._route,
                afunc=self._aroute,
                writer=writer,
                reader=reader,
                name=None,
                trace=False,
            )
        )

    def _route(
        self,
        input: Any,
        config: RunnableConfig,
        *,
        reader: Optional[Callable[[], Any]],
        writer: Callable[[list[str]], Optional[Runnable]],
    ) -> Runnable:
        result = self.path.invoke(reader(config) if reader else input, config)
        if not isinstance(result, list):
            result = [result]
        if self.ends:
            destinations = [self.ends[r] for r in result]
        else:
            destinations = result
        return writer(destinations) or input

    async def _aroute(
        self,
        input: Any,
        config: RunnableConfig,
        *,
        reader: Optional[Callable[[], Any]],
        writer: Callable[[list[str]], Optional[Runnable]],
    ) -> Runnable:
        result = await self.path.ainvoke(reader(config) if reader else input, config)
        if not isinstance(result, list):
            result = [result]
        if self.ends:
            destinations = [self.ends[r] for r in result]
        else:
            destinations = result
        return writer(destinations) or input


class Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, Runnable] = {}
        self.edges = set[tuple[str, str]]()
        self.branches: defaultdict[str, dict[str, Branch]] = defaultdict(dict)
        self.support_multiple_edges = False
        self.compiled = False

    @property
    def _all_edges(self) -> set[tuple[str, str]]:
        return self.edges

    def add_node(self, key: str, action: RunnableLike) -> None:
        if self.compiled:
            logger.warning(
                "Adding a node to a graph that has already been compiled. This will "
                "not be reflected in the compiled graph."
            )
        if key in self.nodes:
            raise ValueError(f"Node `{key}` already present.")
        if key == END or key == START:
            raise ValueError(f"Node `{key}` is reserved.")

        self.nodes[key] = coerce_to_runnable(action, name=key, trace=False)

    def add_edge(self, start_key: str, end_key: str) -> None:
        if self.compiled:
            logger.warning(
                "Adding an edge to a graph that has already been compiled. This will "
                "not be reflected in the compiled graph."
            )
        if start_key == END:
            raise ValueError("END cannot be a start node")
        if end_key == START:
            raise ValueError("START cannot be an end node")
        if not self.support_multiple_edges and start_key in set(
            start for start, _ in self.edges
        ):
            raise ValueError(
                f"Already found path for node '{start_key}'.\n"
                "For multiple edges, use StateGraph with an annotated state key."
            )

        self.edges.add((start_key, end_key))

    def add_conditional_edges(
        self,
        source: str,
        path: Union[
            Callable[..., Union[str, list[str]]],
            Callable[..., Awaitable[Union[str, list[str]]]],
            Runnable[Any, Union[str, list[str]]],
        ],
        path_map: Optional[dict[str, str]] = None,
        then: Optional[str] = None,
    ) -> None:
        """Add a conditional edge from the starting node to any number of destination nodes.

        Args:
            source (str): The starting node. This conditional edge will run when
                exiting this node.
            path (Union[Callable, Runnable]): The callable that determines the next
                node or nodes. If not specifying `path_map` it should return one or
                more nodes. If it returns END, the graph will stop execution.
            path_map (Optional[dict[str, str]]): Optional mapping of paths to node
                names. If ommitted the paths returned by `path` should be node names.
            then (Optional[str]): The name of a node to execute after the nodes
                selected by `path`.

        Returns:
            None
        """  # noqa: E501
        if self.compiled:
            logger.warning(
                "Adding an edge to a graph that has already been compiled. This will "
                "not be reflected in the compiled graph."
            )
        # find a name for the condition
        path = coerce_to_runnable(path, name=None, trace=True)
        name = path.name or "condition"
        # validate the condition
        if name in self.branches[source]:
            raise ValueError(
                f"Branch with name `{path.name}` already exists for node " f"`{source}`"
            )
        # save it
        self.branches[source][name] = Branch(path, path_map, then)

    def set_entry_point(self, key: str) -> None:
        """Specifies the first node to be called in the graph.

        Parameters:
            key (str): The key of the node to set as the entry point.

        Returns:
            None
        """
        return self.add_edge(START, key)

    def set_conditional_entry_point(
        self,
        path: Union[
            Callable[..., str], Callable[..., Awaitable[str]], Runnable[Any, str]
        ],
        path_map: Optional[Dict[str, str]] = None,
        then: Optional[str] = None,
    ) -> None:
        """Sets a conditional entry point in the graph.

        Args:
            path (Union[Callable, Runnable]): The callable that determines the next
                node or nodes. If not specifying `path_map` it should return one or
                more nodes. If it returns END, the graph will stop execution.
            path_map (Optional[dict[str, str]]): Optional mapping of paths to node
                names. If ommitted the paths returned by `path` should be node names.
            then (Optional[str]): The name of a node to execute after the nodes
                selected by `path`.

        Returns:
            None
        """
        return self.add_conditional_edges(START, path, path_map, then)

    def set_finish_point(self, key: str) -> None:
        """Marks a node as a finish point of the graph.

        If the graph reaches this node, it will cease execution.

        Parameters:
            key (str): The key of the node to set as the finish point.

        Returns:
            None
        """
        return self.add_edge(key, END)

    def validate(self, interrupt: Optional[Sequence[str]] = None) -> None:
        # assemble sources
        all_sources = {src for src, _ in self._all_edges}
        for start, branches in self.branches.items():
            for cond, branch in branches.items():
                all_sources.add(start)
                if branch.then is not None:
                    if branch.ends is not None:
                        for end in branch.ends.values():
                            all_sources.add(end)
                    else:
                        for node in self.nodes:
                            if node != start and node != branch.then:
                                all_sources.add(node)
        # validate sources
        for node in self.nodes:
            if node not in all_sources:
                raise ValueError(f"Node '{node}' is a dead-end")
        for source in all_sources:
            if node not in self.nodes and node != START:
                raise ValueError(f"Found edge starting at unkown node '{source}'")

        # assemble targets
        all_targets = {end for _, end in self._all_edges}
        for start, branches in self.branches.items():
            for cond, branch in branches.items():
                if branch.then is not None:
                    all_targets.add(branch.then)
                if branch.ends is not None:
                    for end in branch.ends.values():
                        if end not in self.nodes and end != END:
                            raise ValueError(
                                f"At '{start}' node, '{cond}' branch found unknown target '{end}'"
                            )
                        all_targets.add(end)
                else:
                    all_targets.add(END)
                    for node in self.nodes:
                        if node != start and node != branch.then:
                            all_targets.add(node)
        # validate targets
        for node in self.nodes:
            if node not in all_targets:
                raise ValueError(f"Node `{node}` is not reachable")
        for target in all_targets:
            if target not in self.nodes and target != END:
                raise ValueError(f"Found edge ending at unknown node `{target}`")
        # validate interrupts
        if interrupt:
            for node in interrupt:
                if node not in self.nodes:
                    raise ValueError(f"Interrupt node `{node}` not found")

        self.compiled = True

    def compile(
        self,
        checkpointer: Optional[BaseCheckpointSaver] = None,
        interrupt_before: Optional[Sequence[str]] = None,
        interrupt_after: Optional[Sequence[str]] = None,
        debug: bool = False,
    ) -> "CompiledGraph":
        # assign default values
        interrupt_before = interrupt_before or []
        interrupt_after = interrupt_after or []

        # validate the graph
        self.validate(interrupt=interrupt_before + interrupt_after)

        # create empty compiled graph
        compiled = CompiledGraph(
            graph=self,
            nodes={},
            channels={START: EphemeralValue(Any), END: EphemeralValue(Any)},
            input_channels=START,
            output_channels=END,
            stream_mode="values",
            stream_channels=[],
            checkpointer=checkpointer,
            interrupt_before_nodes=interrupt_before,
            interrupt_after_nodes=interrupt_after,
            auto_validate=False,
            debug=debug,
        )

        # attach nodes, edges, and branches
        for key, node in self.nodes.items():
            compiled.attach_node(key, node)

        for start, end in self.edges:
            compiled.attach_edge(start, end)

        for start, branches in self.branches.items():
            for name, branch in branches.items():
                compiled.attach_branch(start, name, branch)

        # validate the compiled graph
        return compiled.validate()


class CompiledGraph(Pregel):
    graph: Graph

    def attach_node(self, key: str, node: Runnable) -> None:
        self.channels[key] = EphemeralValue(Any)
        self.nodes[key] = (
            PregelNode(channels=[], triggers=[])
            | node
            | ChannelWrite([ChannelWriteEntry(key)], tags=[TAG_HIDDEN])
        )
        cast(list[str], self.stream_channels).append(key)

    def attach_edge(self, start: str, end: str) -> None:
        if end == END:
            # publish to end channel
            self.nodes[start].writers.append(
                ChannelWrite([ChannelWriteEntry(END)], tags=[TAG_HIDDEN])
            )
        else:
            # subscribe to start channel
            self.nodes[end].triggers.append(start)
            self.nodes[end].channels.append(start)

    def attach_branch(self, start: str, name: str, branch: Branch) -> None:
        def branch_writer(ends: list[str]) -> Optional[ChannelWrite]:
            channels = [
                f"branch:{start}:{name}:{end}" if end != END else END for end in ends
            ]
            return ChannelWrite(
                [ChannelWriteEntry(ch) for ch in channels], tags=[TAG_HIDDEN]
            )

        # add hidden start node
        if start == START and start not in self.nodes:
            self.nodes[start] = Channel.subscribe_to(START, tags=[TAG_HIDDEN])

        # attach branch writer
        self.nodes[start] |= branch.run(branch_writer)

        # attach branch readers
        ends = branch.ends.values() if branch.ends else [node for node in self.nodes]
        for end in ends:
            if end != END:
                channel_name = f"branch:{start}:{name}:{end}"
                self.channels[channel_name] = EphemeralValue(Any)
                self.nodes[end].triggers.append(channel_name)
                self.nodes[end].channels.append(channel_name)

    def get_graph(
        self,
        config: Optional[RunnableConfig] = None,
        *,
        xray: Union[int, bool] = False,
        add_condition_nodes: bool = True,
    ) -> DrawableGraph:
        """Returns a drawable representation of the computation graph."""
        graph = DrawableGraph()
        start_nodes: dict[str, RunnableGraphNode] = {
            START: graph.add_node(self.get_input_schema(config), START)
        }
        end_nodes: dict[str, RunnableGraphNode] = {
            END: graph.add_node(self.get_output_schema(config), END)
        }

        for key, node in self.graph.nodes.items():
            if xray:
                subgraph = (
                    node.get_graph(
                        config=config,
                        xray=xray - 1 if isinstance(xray, int) and xray > 0 else xray,
                    )
                    if isinstance(node, CompiledGraph)
                    else node.get_graph(config=config)
                )
                subgraph.trim_first_node()
                subgraph.trim_last_node()
                if len(subgraph.nodes) > 1:
                    end_nodes[key], start_nodes[key] = graph.extend(
                        subgraph, prefix=key
                    )
                else:
                    n = graph.add_node(node, key)
                    start_nodes[key] = n
                    end_nodes[key] = n
            else:
                n = graph.add_node(node, key)
                start_nodes[key] = n
                end_nodes[key] = n
        for start, end in sorted(self.graph._all_edges):
            graph.add_edge(start_nodes[start], end_nodes[end])
        branches_by_name = Counter(
            name for _, branches in self.graph.branches.items() for name in branches
        )
        for start, branches in self.graph.branches.items():
            default_ends = {
                **{k: k for k in self.graph.nodes if k != start},
                END: END,
            }
            for name, branch in branches.items():
                if branch.ends is not None:
                    ends = branch.ends
                elif branch.then is not None:
                    ends = {k: k for k in default_ends if k not in (END, branch.then)}
                else:
                    ends = default_ends

                if add_condition_nodes is True:
                    cond = graph.add_node(
                        branch.path,
                        f"{start}_{name}" if branches_by_name[name] > 1 else name,
                    )
                    graph.add_edge(start_nodes[start], cond)
                    for label, end in ends.items():
                        graph.add_edge(cond, end_nodes[end], label, conditional=True)
                        if branch.then is not None:
                            graph.add_edge(start_nodes[end], end_nodes[branch.then])
                else:
                    for label, end in ends.items():
                        graph.add_edge(
                            start_nodes[start], end_nodes[end], label, conditional=True
                        )
                        if branch.then is not None:
                            graph.add_edge(start_nodes[end], end_nodes[branch.then])

        return graph

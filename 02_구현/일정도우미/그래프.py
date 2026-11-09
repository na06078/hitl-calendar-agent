"""interrupt 노드는 재개 시 처음부터 다시 실행됩니다. 쓰기는 별도 execute 노드입니다."""
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt

class JobState(TypedDict, total=False):
    job_id: str
    route: str
    decision: dict


def build_graph(saver, assess, apply, execute, view):
    def check(state):
        return {'route': assess(state['job_id'])}
    def review(state):
        response = interrupt(view(state['job_id']))
        return {'decision': response}
    def respond(state):
        return {'route': apply(state['job_id'], state['decision'])}
    def run(state):
        return {'route': execute(state['job_id'])}
    graph = StateGraph(JobState)
    graph.add_node('assess', check)
    graph.add_node('review', review)
    graph.add_node('respond', respond)
    graph.add_node('execute', run)
    graph.add_edge(START, 'assess')
    graph.add_conditional_edges('assess', lambda s: s['route'], {'review': 'review', 'execute': 'execute'})
    graph.add_edge('review', 'respond')
    graph.add_conditional_edges('respond', lambda s: s['route'], {'end': END, 'assess': 'assess', 'execute': 'execute'})
    graph.add_conditional_edges('execute', lambda s: s['route'], {'end': END, 'review': 'review'})
    return graph.compile(checkpointer=saver)

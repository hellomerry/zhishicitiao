from src.pipeline.nodes import execute_node, NODES


async def run_pipeline(task_id, node_inputs: dict | None = None) -> list:
    results = []
    inputs = node_inputs or {}
    for node_name in NODES[:3]:  # 阶段 0 只跑前 3 节点
        r = await execute_node(task_id, node_name, inputs.get(node_name, {}))
        results.append({"node": node_name, "result": r})
    return results

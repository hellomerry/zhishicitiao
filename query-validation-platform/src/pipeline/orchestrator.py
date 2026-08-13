from src.pipeline.nodes import (
    execute_node, NODES, node_draft_gen, node_rule_check, node_page_split,
    node_asset_gen, node_ocr_read, node_cross_check,
    node_risk_classify, node_review_queue, node_batch_signoff, node_publish_snapshot,
)

NODE_FN = {
    "draft_gen": node_draft_gen,
    "rule_check": node_rule_check,
    "page_split": node_page_split,
    "asset_gen": node_asset_gen,
    "ocr_read": node_ocr_read,
    "cross_check": node_cross_check,
    "risk_classify": node_risk_classify,
    "review_queue": node_review_queue,
    "batch_signoff": node_batch_signoff,
    "publish_snapshot": node_publish_snapshot,
}


async def run_pipeline(task_id, node_inputs: dict | None = None) -> list:
    results = []
    inputs = node_inputs or {"task_id": task_id}
    for node_name in NODES:
        fn = NODE_FN.get(node_name)
        r = await execute_node(task_id, node_name, inputs, fn)
        results.append({"node": node_name, "result": r})
    return results

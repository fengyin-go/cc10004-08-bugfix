"""一次性改造：把各路由里直接从 values 取 action 的旧写法替换为 extract_action。"""
from __future__ import annotations

import pathlib

ROUTERS = pathlib.Path("/workspace/backend/app/routers")

for path in sorted(ROUTERS.glob("*.py")):
    text = path.read_text(encoding="utf-8")
    old_import = "from app.schemas import ActionResult, EntryPayload, PageResult\n"
    new_import = (
        "from app.schemas import ActionResult, EntryPayload, PageResult, extract_action\n"
    )
    old_line = '    action = str(payload.values.get("action") or "").strip()\n'
    new_line = "    action = extract_action(payload)\n"
    if old_line not in text:
        print(f"skip {path.name}: action line not found")
        continue
    text = text.replace(old_line, new_line)
    if old_import in text:
        text = text.replace(old_import, new_import)
    elif "extract_action" not in text:
        raise SystemExit(f"import anchor missing in {path.name}")
    path.write_text(text, encoding="utf-8")
    print(f"updated {path.name}")

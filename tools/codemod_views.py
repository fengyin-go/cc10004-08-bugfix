"""一次性改造：通用模块页面改用 postAction，确保后端业务拒绝原因直达页脚。

跳过 pipeline / scheduler 两个需要定制页面的模块。
"""
from __future__ import annotations

import pathlib
import re

VIEWS = pathlib.Path("/workspace/frontend/src/views")
SKIP = {"pipeline", "scheduler"}

PATTERN = re.compile(
    r"async function runAction\(action: string, row: Row\) \{.*?\n\}\n",
    re.DOTALL,
)
REPLACEMENT = """async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    await postAction(`${ENDPOINT}/${row.id}/actions`, action)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}
"""

for path in sorted(VIEWS.glob("*/index.vue")):
    module = path.parent.name
    if module in SKIP:
        print(f"skip {module}")
        continue
    text = path.read_text(encoding="utf-8")
    if "postAction" in text:
        print(f"already {module}")
        continue
    new_text, count = PATTERN.subn(REPLACEMENT, text, count=1)
    if count != 1:
        raise SystemExit(f"runAction block not matched in {module}")
    new_text = new_text.replace(
        "import { request } from '@/api/client'",
        "import { postAction, request } from '@/api/client'",
        1,
    )
    path.write_text(new_text, encoding="utf-8")
    print(f"updated {module}")

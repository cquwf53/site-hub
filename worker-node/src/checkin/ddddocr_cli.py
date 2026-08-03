#!/usr/bin/env python
"""
ddddocr 常驻识别服务，供 Node 端通过 stdin/stdout 行协议调用。

协议：
  - 启动完成后向 stdout 打印一行 JSON：{"ready": true} 或 {"ready": false, "error": "..."}
  - 之后每次从 stdin 读入一行 base64（不含 data: 前缀），识别后回一行 JSON：
      {"ok": true, "text": "结果"} 或 {"ok": false, "error": "..."}
  - 读到 "__EXIT__" 或 EOF 时退出。

仅用 stdout 传结果，import/初始化的告警都走 stderr，避免污染协议。
"""

import sys
import json
import base64


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    try:
        import ddddocr
    except Exception as exc:  # noqa: BLE001
        emit({"ready": False, "error": "ddddocr import failed: %s" % exc})
        return

    try:
        ocr = ddddocr.DdddOcr(show_ad=False)
    except Exception as exc:  # noqa: BLE001
        emit({"ready": False, "error": "ddddocr init failed: %s" % exc})
        return

    emit({"ready": True})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if line == "__EXIT__":
            break
        try:
            image = base64.b64decode(line)
            text = ocr.classification(image)
            emit({"ok": True, "text": text})
        except Exception as exc:  # noqa: BLE001
            emit({"ok": False, "error": str(exc)})


if __name__ == "__main__":
    main()

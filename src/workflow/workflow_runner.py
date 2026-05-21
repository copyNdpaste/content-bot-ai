#!/usr/bin/env python3
# version: workflow_runner_v1
"""수동 즉시 회차 트리거.

스케줄러 안 기다리고 지금 1회 돌리고 싶을 때 사용한다.
F5 후 박재범 채팅에서 "/start-routine" 같은 사용자 액션이나,
사장님 터미널에서 한 줄로 실행 가능.

CLI 인자는 content_pipeline.py 와 동일하게 위임된다.

예:
  python3 workflow_runner.py --platform threads --account jp
  python3 workflow_runner.py --platform all --account all --theme "K-뷰티 봄 신상"
  python3 workflow_runner.py --platform x --account kr --dry-run
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PIPELINE = os.path.join(HERE, "content_pipeline.py")
PYTHON_BIN = sys.executable or "/opt/homebrew/bin/python3"


def main() -> int:
    if not os.path.isfile(PIPELINE):
        sys.stderr.write(f"❌ pipeline 없음: {PIPELINE}\n")
        return 1
    args = [PYTHON_BIN, PIPELINE] + sys.argv[1:]
    # subprocess 가 stdout/stderr 그대로 흘리도록 capture X
    proc = subprocess.run(args)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())

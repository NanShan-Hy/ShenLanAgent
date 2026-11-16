#!/usr/bin/env python3
"""Optional cross-service contract check with existing isolated test dependencies.

Uses Gateway's interpreter for its document subprocesses; installs nothing and
never reads production configuration, keys, databases or real provider accounts.
"""
import os
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
GATEWAY=ROOT.parent/'ai-gateway'
python=GATEWAY/'.venv/bin/python'
if not python.is_file():raise SystemExit('需先准备独立AI Gateway测试环境')
if Path(sys.prefix).resolve()!=(GATEWAY/'.venv').resolve():
    os.execv(str(python),[str(python),str(Path(__file__).resolve()),*sys.argv[1:]])
sys.path.insert(0,str(ROOT/'src'))
sys.path.append(str(ROOT/'.venv/lib/python3.12/site-packages'))
os.chdir(ROOT)
import pytest
default_temp='/home/nanshan/workspace/tmp/autotest-platform/gateway-contract-'+str(time.time_ns())
raise SystemExit(pytest.main(['-q','tests/test_ai_gateway_contract.py','--basetemp='+default_temp,
    '--junitxml=/home/nanshan/workspace/data/autotest-platform/dev/qa-v13-gateway.xml','-p','no:cacheprovider',*sys.argv[1:]]))

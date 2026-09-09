"""Exercise async filter races against the actual dashboard loader."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_latest_range_response_wins_even_when_old_response_is_slower():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed to execute the frontend loader')
    app = Path(__file__).resolve().parents[1] / 'web' / 'app.js'
    script = r'''
const fs = require('fs'), vm = require('vm');
const text = fs.readFileSync(process.argv[1], 'utf8');
const code = text.slice(text.indexOf('let dashboardRequestSerial'), text.indexOf('function setLoading'));
const pending = [];
let renders = 0;
const context = {
  URLSearchParams, elements:{},
  state:{range:'all',agent:'all',loading:false,preview:false},
  getJson:url => url.startsWith('/api/dashboard') ? new Promise(resolve => pending.push(resolve)) : Promise.resolve({}),
  setLoading:value => context.state.loading=value,
  setConnection:() => {}, render:() => {renders++}, startPolling:() => {},
  mockDashboard:() => {throw Error('Unexpected error path')},
};
vm.createContext(context); vm.runInContext(code, context);
(async () => {
  const old = context.loadDashboard();
  context.state.range='1';
  const latest = context.loadDashboard({quiet:true});
  pending[1]({summary:{total:150},meta:{scan:{status:'ready'}}});
  await latest;
  pending[0]({summary:{total:650},meta:{scan:{status:'ready'}}});
  await old;
  if (context.state.data.summary.total !== 150 || renders !== 1 || context.state.loading)
    throw Error('A stale response replaced the selected range');
})().catch(error => {console.error(error); process.exitCode=1});
'''
    result = subprocess.run([node, '-e', script, str(app)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr

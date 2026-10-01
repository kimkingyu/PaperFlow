import { spawn } from 'node:child_process';
import { existsSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const root = fileURLToPath(new URL('../../',import.meta.url));
const output = path.join(root,'frontend','test-results','backend');
// Isolated test files stay under frontend; no user database or model credential is opened.
mkdirSync(output,{recursive:true});
const code = `import tempfile\nimport uvicorn\nfrom paperflow.application.services import ApplicationServices\nfrom paperflow.gui.server import create_app\nwith tempfile.TemporaryDirectory(dir=${JSON.stringify(output)}) as directory:\n    services = ApplicationServices(data_dir=directory)\n    app = create_app(token='isolated-real-backend-token', port=5179, finder=services.finder, application=services, standalone=True)\n    uvicorn.run(app, host='127.0.0.1', port=5179, log_level='warning')\n`;
const localPython = path.join(root,'.venv',process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const child = spawn(process.env.PAPERFLOW_TEST_PYTHON || (existsSync(localPython) ? localPython : 'python'),['-c',code],{cwd:root,stdio:'inherit'});
child.on('exit',code => process.exit(code ?? 0));
child.on('error',error => {console.error(error.message);process.exit(1);});
for(const signal of ['SIGINT','SIGTERM'])process.on(signal,() => {child.kill();process.exit(0);});

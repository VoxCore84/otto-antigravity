import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';
import { execFile } from 'child_process';

interface Baseline {
    settings_critical: Record<string, any>;
    argv_critical: Record<string, any>;
    argv_js_flags_required: string[];
    disabled_extensions: string[];
}

interface CheckResult {
    name: string;
    status: 'OK' | 'WARN' | 'ERROR';
    detail: string;
}

// Path resolution (cross-platform, configurable)
function getHome(): string {
    return process.env.HOME || process.env.USERPROFILE || '';
}

function getAntigravityDataDir(): string {
    if (process.platform === 'win32') {
        return path.join(process.env.APPDATA || path.join(getHome(), 'AppData', 'Roaming'), 'Antigravity');
    } else if (process.platform === 'darwin') {
        return path.join(getHome(), 'Library', 'Application Support', 'Antigravity');
    }
    return path.join(getHome(), '.config', 'Antigravity');
}

function getAntigravityInstallDir(): string {
    if (process.env.ANTIGRAVITY_INSTALL_DIR) return process.env.ANTIGRAVITY_INSTALL_DIR;
    if (process.platform === 'win32') {
        return path.join(process.env.LOCALAPPDATA || path.join(getHome(), 'AppData', 'Local'), 'Programs', 'Antigravity');
    } else if (process.platform === 'darwin') {
        return '/Applications/Antigravity.app/Contents';
    }
    return '/usr/share/antigravity';
}

function getPaths() {
    const dataDir = getAntigravityDataDir();
    const installDir = getAntigravityInstallDir();
    return {
        settingsJson: path.join(dataDir, 'User', 'settings.json'),
        argvJson: path.join(getHome(), '.antigravity', 'argv.json'),
        bundledExtDir: path.join(installDir, 'resources', 'app', 'extensions'),
    };
}

let statusBarItem: vscode.StatusBarItem;
let outputChannel: vscode.OutputChannel;
let checkInterval: NodeJS.Timeout | undefined;

export function activate(context: vscode.ExtensionContext) {
    outputChannel = vscode.window.createOutputChannel('OTTO');

    statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 1000);
    statusBarItem.command = 'otto.showReport';
    statusBarItem.show();
    context.subscriptions.push(statusBarItem);

    context.subscriptions.push(
        vscode.commands.registerCommand('otto.checkHealth', () => runHealthCheck()),
        vscode.commands.registerCommand('otto.fixAll', () => runFixAll()),
        vscode.commands.registerCommand('otto.showReport', () => {
            outputChannel.show();
            runHealthCheck();
        })
    );

    context.subscriptions.push(
        vscode.workspace.onDidChangeConfiguration(e => {
            const watchKeys = ['telemetry.telemetryLevel', 'extensions.autoUpdate',
                'editor.minimap.enabled', 'workbench.enableExperiments'];
            if (watchKeys.some(k => e.affectsConfiguration(k))) {
                runHealthCheck();
            }
        })
    );

    runHealthCheck();

    const intervalMinutes = vscode.workspace.getConfiguration('otto').get<number>('checkIntervalMinutes', 30);
    checkInterval = setInterval(() => runHealthCheck(), intervalMinutes * 60 * 1000);
    context.subscriptions.push({ dispose: () => { if (checkInterval) clearInterval(checkInterval); } });

    outputChannel.appendLine('[OTTO] Activated');
}

export function deactivate() {
    if (checkInterval) clearInterval(checkInterval);
}

function findBaseline(): string | null {
    const config = vscode.workspace.getConfiguration('otto');
    const toolkitDir = config.get<string>('toolkitDir', '');

    // Check configured toolkit dir
    if (toolkitDir) {
        const candidate = path.join(toolkitDir, 'baseline.json');
        if (fs.existsSync(candidate)) return candidate;
    }

    // Check workspace folders
    for (const folder of vscode.workspace.workspaceFolders || []) {
        const candidate = path.join(folder.uri.fsPath, 'baseline.json');
        if (fs.existsSync(candidate)) return candidate;
    }

    // Check next to the extension
    const extBaseline = path.join(path.dirname(__dirname), 'baseline.json');
    if (fs.existsSync(extBaseline)) return extBaseline;

    return null;
}

function loadBaseline(): Baseline | null {
    const bp = findBaseline();
    if (!bp) return null;
    try {
        return JSON.parse(fs.readFileSync(bp, 'utf-8'));
    } catch {
        return null;
    }
}

async function runHealthCheck(): Promise<void> {
    const baseline = loadBaseline();
    if (!baseline) {
        updateStatusBar('error', 'No baseline');
        outputChannel.appendLine('[OTTO] ERROR: baseline.json not found');
        return;
    }

    const results: CheckResult[] = [];
    const timestamp = new Date().toLocaleTimeString();
    const ap = getPaths();

    // 1. Settings
    try {
        const settings = JSON.parse(fs.readFileSync(ap.settingsJson, 'utf-8'));
        let ok = true;
        for (const [key, expected] of Object.entries(baseline.settings_critical)) {
            const actual = getNestedValue(settings, key);
            if (JSON.stringify(actual) !== JSON.stringify(expected)) {
                results.push({ name: `Setting: ${key}`, status: 'WARN',
                    detail: `Expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}` });
                ok = false;
            }
        }
        if (ok) results.push({ name: 'Settings', status: 'OK',
            detail: `${Object.keys(baseline.settings_critical).length} keys verified` });
    } catch (err) {
        results.push({ name: 'Settings', status: 'ERROR', detail: `${err}` });
    }

    // 2. argv.json
    try {
        const argv = JSON.parse(fs.readFileSync(ap.argvJson, 'utf-8'));
        let ok = true;
        for (const [key, expected] of Object.entries(baseline.argv_critical)) {
            if (JSON.stringify(argv[key]) !== JSON.stringify(expected)) {
                results.push({ name: `argv: ${key}`, status: 'WARN',
                    detail: `Expected ${JSON.stringify(expected)}, got ${JSON.stringify(argv[key])}` });
                ok = false;
            }
        }
        const jsFlags = argv['js-flags'] || '';
        for (const flag of baseline.argv_js_flags_required) {
            if (!jsFlags.includes(flag)) {
                results.push({ name: 'argv: js-flags', status: 'WARN', detail: `Missing: ${flag}` });
                ok = false;
            }
        }
        if (ok) results.push({ name: 'argv.json', status: 'OK', detail: 'All flags verified' });
    } catch (err) {
        results.push({ name: 'argv.json', status: 'ERROR', detail: `${err}` });
    }

    // 3. Extensions
    try {
        const entries = fs.readdirSync(ap.bundledExtDir);
        const reenabled = baseline.disabled_extensions.filter(
            ext => entries.includes(ext) && !entries.includes(ext + '.disabled')
        );
        if (reenabled.length > 0) {
            results.push({ name: 'Extensions', status: 'ERROR',
                detail: `${reenabled.length} re-enabled: ${reenabled.slice(0, 5).join(', ')}` });
        } else {
            results.push({ name: 'Extensions', status: 'OK',
                detail: `${baseline.disabled_extensions.length} still disabled` });
        }
    } catch (err) {
        results.push({ name: 'Extensions', status: 'ERROR', detail: `${err}` });
    }

    // Overall
    const errors = results.filter(r => r.status === 'ERROR').length;
    const warns = results.filter(r => r.status === 'WARN').length;
    const oks = results.filter(r => r.status === 'OK').length;

    const overall = errors > 0 ? 'critical' : warns > 0 ? 'degraded' : 'healthy';
    updateStatusBar(overall === 'healthy' ? 'ok' : overall === 'degraded' ? 'warn' : 'error',
        `${oks}/${results.length} OK`);

    outputChannel.appendLine('');
    outputChannel.appendLine(`=== OTTO Health Check [${timestamp}] ===`);
    outputChannel.appendLine(`Status: ${overall.toUpperCase()} (${oks} OK, ${warns} WARN, ${errors} ERROR)`);
    for (const r of results) {
        const icon = r.status === 'OK' ? '[OK]' : r.status === 'WARN' ? '[!!]' : '[ER]';
        outputChannel.appendLine(`  ${icon} ${r.name}: ${r.detail}`);
    }
    outputChannel.appendLine('');

    if (overall === 'critical') {
        const action = await vscode.window.showWarningMessage(
            `OTTO: ${errors} regression(s) detected`, 'Fix All', 'Show Report');
        if (action === 'Fix All') runFixAll();
        else if (action === 'Show Report') outputChannel.show();
    }
}

function updateStatusBar(status: 'ok' | 'warn' | 'error', text: string) {
    if (status === 'ok') {
        statusBarItem.text = '$(check) OTTO';
        statusBarItem.tooltip = `OTTO: All optimizations healthy (${text})`;
        statusBarItem.backgroundColor = undefined;
    } else if (status === 'warn') {
        statusBarItem.text = '$(warning) OTTO';
        statusBarItem.tooltip = `OTTO: Minor issues (${text})`;
        statusBarItem.backgroundColor = new vscode.ThemeColor('statusBarItem.warningBackground');
    } else {
        statusBarItem.text = '$(error) OTTO';
        statusBarItem.tooltip = `OTTO: Regressions detected! (${text})`;
        statusBarItem.backgroundColor = new vscode.ThemeColor('statusBarItem.errorBackground');
    }
}

async function runFixAll(): Promise<void> {
    outputChannel.appendLine('[OTTO] Running auto-fix...');
    const python = vscode.workspace.getConfiguration('otto').get<string>('pythonPath', 'python');

    try {
        const result = await runPython(python, ['-m', 'otto.optimizer', '--fix']);
        outputChannel.appendLine(result);
    } catch (err) {
        outputChannel.appendLine(`[OTTO] Optimizer error: ${err}`);
    }

    try {
        const result = await runPython(python, ['-m', 'otto.extensions']);
        outputChannel.appendLine(result);
    } catch (err) {
        outputChannel.appendLine(`[OTTO] Extensions error: ${err}`);
    }

    outputChannel.appendLine('[OTTO] Re-checking...');
    await runHealthCheck();
    vscode.window.showInformationMessage('OTTO: Fix complete -- see output for details');
}

function runPython(python: string, args: string[]): Promise<string> {
    return new Promise((resolve, reject) => {
        execFile(python, args, { timeout: 30000 }, (error, stdout, stderr) => {
            if (error) reject(`${error.message}\n${stderr}`);
            else resolve(stdout + (stderr ? `\n${stderr}` : ''));
        });
    });
}

function getNestedValue(obj: any, key: string): any {
    let current = obj;
    for (const part of key.split('.')) {
        if (current === undefined || current === null) return undefined;
        current = current[part];
    }
    return current;
}

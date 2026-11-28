// Talks to the voice helper over its control socket (~/.voice-bridge/control.sock), the same
// one the `voice` command uses. The worker runs as a normal Node process, so it can open it.
import { execFile, spawn } from 'node:child_process'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'
import net from 'node:net'
import os from 'node:os'
import path from 'node:path'
import type { OrcaWorkerApi } from 'orca/worker'
import type { VoiceStatus } from './protocol'

const SOCK = path.join(os.homedir(), '.voice-bridge', 'control.sock')
const DOWN: VoiceStatus = {
  up: false, phase: '', needsSetup: false, note: '', hook: false, inputVolume: -1, inputDevice: '', key: '', mode: '', stt: '', voice: '', speed: '', speaker: '', mic: '', conversation: '',
  silence: '', enter: '', pause: '', following: ''
}

function ask(line: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const s = net.createConnection(SOCK)
    let out = ''
    s.setTimeout(20000, () => { s.destroy(); reject(new Error('timeout')) })
    s.on('connect', () => s.write(line + '\n'))
    s.on('data', (d) => { out += d })
    s.on('end', () => resolve(out.trim()))
    s.on('error', reject)
  })
}

export function parse(text: string): VoiceStatus {
  const get = (key: string): string => {
    const m = text.match(new RegExp('^' + key + ': (.*)$', 'm'))
    return m ? m[1].trim() : ''
  }
  return {
    up: true,
    phase: '',
    needsSetup: false,
    note: '',
    hook: false,
    inputVolume: -1,
    inputDevice: '',
    key: get('talk key'),
    mode: get('mode'),
    stt: get('speech-to-text'),
    voice: get('voice'),
    speed: get('voice speed').replace('x', ''),
    speaker: get('speaker'),
    mic: get('microphone'),
    conversation: get('hands-free conversation'),
    silence: get('stop double-tap recording after silence'),
    enter: get('press Enter after local dictation'),
    pause: get('pause other audio while dictating'),
    following: get('following')
  }
}

// Set while a start, stop or restart runs, so the panel can show it. In memory only: the
// helper itself is the source of truth once it answers.
let phase: VoiceStatus['phase'] = ''
// This file sits in <plugin>/src, so the plugin's own `voice` command is one folder up, in bin/.
const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)))
const VOICE = path.join(ROOT, 'bin', 'voice')
const SETUP_LOG = path.join(os.homedir(), '.voice-bridge', 'setup.log')
let setupFailed = false
const needsSetup = (): boolean => !fs.existsSync(path.join(os.homedir(), '.voice-bridge', 'venv'))

function tail(file: string, lines: number): string {
  try {
    return fs.readFileSync(file, 'utf8').trim().split('\n').slice(-lines).join('\n')
  } catch {
    return ''
  }
}

// Read at call time, so a test can point it at a temporary file.
const settingsFile = (): string => process.env.VOICE_MODE_CLAUDE_SETTINGS ?? path.join(os.homedir(), '.claude', 'settings.json')
const HOOK_COMMAND = path.join(ROOT, 'bin', 'voice-hook')

type HookGroup = { matcher?: string; hooks?: { type?: string; command?: string }[] }
const isVoiceHook = (h: { command?: string }): boolean => typeof h.command === 'string' && h.command.includes('voice-hook')

function readClaudeSettings(): Record<string, unknown> | null {
  try {
    return JSON.parse(fs.readFileSync(settingsFile(), 'utf8'))
  } catch {
    return fs.existsSync(settingsFile()) ? null : {}  // null: present but unreadable, so do not touch it
  }
}

export function hookInstalled(): boolean {
  const groups = ((readClaudeSettings()?.hooks as Record<string, HookGroup[]> | undefined)?.UserPromptSubmit) ?? []
  return groups.some((g) => (g.hooks ?? []).some(isVoiceHook))
}

/** Adds or removes only this plugin's entry. Other hooks (Orca keeps its own there) are left alone. */
export function setHook(enable: boolean): void {
  const settings = readClaudeSettings()
  if (!settings) return
  const hooks = ((settings.hooks as Record<string, HookGroup[]> | undefined) ?? {})
  let groups = hooks.UserPromptSubmit ?? []
  const present = groups.some((g) => (g.hooks ?? []).some(isVoiceHook))
  if (enable === present) return
  if (fs.existsSync(settingsFile())) {
    fs.copyFileSync(settingsFile(), `${settingsFile()}.bak-voice-mode-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`)
  }
  if (enable) {
    groups = [...groups, { hooks: [{ type: 'command', command: HOOK_COMMAND, timeout: 5 } as { type: string; command: string }] }]
  } else {
    groups = groups
      .map((g) => ({ ...g, hooks: (g.hooks ?? []).filter((h) => !isVoiceHook(h)) }))
      .filter((g) => (g.hooks ?? []).length > 0)
  }
  if (groups.length > 0) hooks.UserPromptSubmit = groups
  else delete hooks.UserPromptSubmit
  settings.hooks = hooks
  fs.mkdirSync(path.dirname(settingsFile()), { recursive: true })
  fs.writeFileSync(settingsFile(), JSON.stringify(settings, null, 2) + '\n')
}

function run1(cmd: string, args: string[]): Promise<string> {
  return new Promise((resolve) => {
    execFile(cmd, args, { timeout: 5000 }, (err, stdout) => resolve(err ? '' : String(stdout).trim()))
  })
}

/** The Mac's current input device and its volume. The volume is -1 when the device has none (AppleScript says "missing value"). */
async function readInput(): Promise<{ inputVolume: number; inputDevice: string }> {
  const [vol, profile] = await Promise.all([
    run1('/usr/bin/osascript', ['-e', 'input volume of (get volume settings)']),
    run1('/usr/sbin/system_profiler', ['SPAudioDataType', '-json'])
  ])
  let inputDevice = ''
  try {
    const items = JSON.parse(profile).SPAudioDataType?.[0]?._items ?? []
    inputDevice = items.find((i: Record<string, string>) => i.coreaudio_default_audio_input_device === 'spaudio_yes')?._name ?? ''
  } catch {
    // keep the empty name
  }
  const n = Number(vol)
  return { inputVolume: vol !== '' && Number.isFinite(n) ? n : -1, inputDevice }
}

async function status(): Promise<VoiceStatus> {
  const input = await readInput()
  const s = await helperStatus()
  return { ...s, ...input }
}

async function helperStatus(): Promise<VoiceStatus> {
  try {
    return { ...parse(await ask('status')), phase: '', hook: hookInstalled() }
  } catch {
    const setup = needsSetup()
    return { ...DOWN, phase, hook: hookInstalled(), needsSetup: setup, note: phase === 'setting up' || setupFailed ? tail(SETUP_LOG, 4) : '' }
  }
}

function run(args: string[]): void {
  // `voice start` launches the helper detached and keeps polling; this only fires it.
  spawn(VOICE, args, { stdio: 'ignore', detached: true }).unref()
}

function runSetup(publish: (s: VoiceStatus) => void): void {
  if (phase) return
  phase = 'setting up'
  setupFailed = false
  fs.mkdirSync(path.dirname(SETUP_LOG), { recursive: true })
  const out = fs.openSync(SETUP_LOG, 'w')
  const child = spawn('/bin/bash', [path.join(ROOT, 'scripts', 'setup.sh')], {
    stdio: ['ignore', out, out],
    env: { ...process.env, VOICE_SETUP_YES: '1' }
  })
  const done = async (ok: boolean): Promise<void> => {
    setupFailed = !ok
    phase = ''
    publish(await status())
  }
  child.on('exit', (code) => void done(code === 0))
  child.on('error', () => void done(false))
  void status().then(publish)
}

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms))

async function waitFor(up: boolean, seconds: number): Promise<boolean> {
  for (let i = 0; i < seconds; i++) {
    if ((await status()).up === up) return true
    await sleep(1000)
  }
  return false
}

async function helper(action: 'start' | 'stop' | 'restart', publish: (s: VoiceStatus) => void): Promise<void> {
  if (phase) return
  try {
    if (action !== 'start') {
      phase = action === 'stop' ? 'stopping' : 'restarting'
      publish(await status())
      run(['quit'])
      await waitFor(false, 15)
    }
    if (action !== 'stop') {
      phase = action === 'start' ? 'starting' : 'restarting'
      publish(await status())
      run(['start'])
      await waitFor(true, 120)  // loading the speech models takes about 30 seconds
    }
  } finally {
    phase = ''
    publish(await status())
  }
}

export default function activate(orca: OrcaWorkerApi): void {
  let last = ''
  let delivered = ''
  const publish = (s: VoiceStatus): void => {
    const key = JSON.stringify(s)
    if (key !== last) {
      last = key
      orca.panels.post('voice.changed', s)
    }
  }

  orca.panels.handle('voice.status', async () => {
    const s = await status()
    publish(s)
    return s
  })

  orca.panels.handle('voice.inputVolume', async ({ value }) => {
    const v = Math.max(0, Math.min(100, Math.round(Number(value))))
    if (Number.isFinite(v)) await run1('/usr/bin/osascript', ['-e', `set volume input volume ${v}`])
    const s = await status()
    publish(s)
    return s
  })

  orca.panels.handle('voice.hook', async ({ enable }) => {
    try {
      setHook(Boolean(enable))
    } catch (e) {
      orca.log(`hook change failed: ${String(e)}`)
    }
    const s = await status()
    publish(s)
    return s
  })

  orca.panels.handle('voice.doctor', async () => {
    try {
      return { text: await ask('doctor') }
    } catch {
      return { text: 'The helper is not running, so it cannot check its own permissions. Start it first.' }
    }
  })

  orca.panels.handle('voice.setup', async () => {
    runSetup(publish)
    await sleep(300)
    return status()
  })

  orca.panels.handle('voice.helper', async ({ action }) => {
    if (action === 'start' || action === 'stop' || action === 'restart') void helper(action, publish)
    await sleep(300)
    return { ...(await status()), phase: phase || '' }
  })

  orca.panels.handle('voice.run', async ({ command }) => {
    // Only plain helper commands: letters, digits, spaces and dots.
    if (!/^[a-z0-9_ .]{1,60}$/i.test(command)) return status()
    try {
      await ask(command)
    } catch {
      // the helper is down; the status below says so
    }
    const s = await status()
    publish(s)
    return s
  })

  // Follow the focused workspace: tell the helper whenever Orca's focus moves to another one.
  // Events can be missed and the helper can be busy, so the timer below re-checks as well, and a
  // focus only counts as delivered once the helper has answered.
  const sendFocus = async (rawId: string | undefined): Promise<void> => {
    const id = rawId?.toLowerCase()
    if (!id || id === delivered) return
    try {
      const reply = await ask(`focus ${id}`)
      if (reply !== 'focus failed') delivered = id
      orca.log(`focus: ${id} -> ${reply.split('\n').pop()}`)
    } catch {
      // the helper is down; the next check tries again
    }
  }
  orca.events.on('workspace.changed', ({ context }) => sendFocus(context?.workspaceId))

  // Keeps open panels current when the state changes from a key, the remote or the terminal.
  timer = setInterval(() => {
    void status().then((st) => {
      if (!st.up) delivered = ''  // a restarted helper has forgotten the focus: send it again
      publish(st)
    })
    void orca.host.call('workspace.readContext').then((c) => sendFocus(c?.workspaceId)).catch(() => {})
  }, 2000)
}

let timer: ReturnType<typeof setInterval> | undefined

export function deactivate(): void {
  if (timer) clearInterval(timer)
}

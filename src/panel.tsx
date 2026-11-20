// Voice controls: the same commands as the `voice` terminal command, as buttons and switches.
import {
  atom, Badge, Button, Card, Code, Heading, plugin, read, Row, Select, set, Stack, Switch, Text,
  type PanelContext
} from 'orca'
import type { VoiceStatus } from './protocol'

const state = atom<VoiceStatus | null>(null)
const doctor = atom<string>('')

async function run(command: string): Promise<void> {
  set(state, await plugin.request('voice.run', { command }))
}

async function choose(v: string): Promise<void> {
  if (v === 'wispr') return run('mode wispr')
  await run(`stt ${v}`)
  return run('mode local')
}

async function runSetup(): Promise<void> {
  set(state, await plugin.request('voice.setup'))
}

async function checkPermissions(): Promise<void> {
  set(doctor, 'Checking...')
  set(doctor, (await plugin.request('voice.doctor')).text)
}

async function chooseKey(key: string): Promise<void> {
  await run(`key ${key}`)
  await helperAction('restart')  // the talk key is read when the helper starts
}

async function helperAction(action: 'start' | 'stop' | 'restart'): Promise<void> {
  set(state, await plugin.request('voice.helper', { action }))
}

export function setup(ctx: PanelContext): void {
  ctx.subscriptions.push(plugin.on('voice.changed', (s) => set(state, s)))
  void plugin.request('voice.status').then((s) => set(state, s))
}

const SPEEDS = ['0.9', '1', '1.15', '1.3', '1.5'].map((v) => ({ value: v, label: `${v}x` }))
const VOICES = [
  { value: 'af_heart', label: 'Heart (US woman, top rated)' },
  { value: 'af_bella', label: 'Bella (US woman)' },
  { value: 'af_nicole', label: 'Nicole (US woman, softer)' },
  { value: 'bf_emma', label: 'Emma (British woman)' },
  { value: 'am_michael', label: 'Michael (US man)' }
]
const SILENCE = ['1.5', '2', '3', '4', '6'].map((v) => ({ value: v, label: `${v} seconds` }))

export default function Panel() {
  const s = read(state)
  if (!s) return <Text muted>Connecting to the voice helper...</Text>
  const busy = s.phase !== ''
  if (!s.up && s.needsSetup) {
    return (
      <Stack gap="md">
        <Row justify="between">
          <Heading>Voice Mode</Heading>
          <Badge tone="neutral">{s.phase === 'setting up' ? 'Setting up...' : 'Setup needed'}</Badge>
        </Row>
        <Text>
          Voice Mode needs a one-time setup: it installs a few tools, builds its Python environment and downloads the
          speech models. This takes a few minutes and needs about 4 GB of space.
        </Text>
        <Button disabled={s.phase === 'setting up'} onPress={() => void runSetup()}>Run setup</Button>
        {s.note ? <Code block>{s.note}</Code> : null}
      </Stack>
    )
  }
  const header = (
    <Stack gap="xs">
      <Row justify="between">
        <Heading>Voice Mode</Heading>
        <Badge tone={s.up ? 'accent' : busy ? 'neutral' : 'danger'}>
          {busy ? `${s.phase}...` : s.up ? 'Running' : 'Not running'}
        </Badge>
      </Row>
      <Row gap="xs">
        <Button disabled={s.up || busy} onPress={() => void helperAction('start')}>Start</Button>
        <Button variant="ghost" disabled={!s.up || busy} onPress={() => void helperAction('stop')}>Stop</Button>
        <Button variant="ghost" disabled={!s.up || busy} onPress={() => void helperAction('restart')}>Restart</Button>
      </Row>
      {busy ? <Text muted size="xs">Loading the speech models takes about 30 seconds.</Text> : null}
    </Stack>
  )
  if (!s.up) return header
  const local = s.mode === 'local'
  const muted = s.voice === 'muted'
  const speed = String(parseFloat(s.speed))
  const silence = s.silence.startsWith('off') ? '' : String(parseFloat(s.silence))
  return (
    <Stack gap="md">
      {header}
      <Card title="Dictation" description="How your voice gets in">
        <Stack gap="md">
          <Select
            label="Dictation"
            labelHidden
            value={local ? s.stt : 'wispr'}
            options={[
              { value: 'wispr', label: 'Wispr Flow' },
              { value: 'parakeet', label: 'Parakeet (local, fast)' },
              { value: 'whisper', label: 'Whisper (local, large v3 turbo)' }
            ]}
            onChange={(v) => void choose(v)}
          />
          <Select
            label="Stop after silence"
            value={silence}
            disabled={!local}
            options={SILENCE}
            onChange={(v) => void run(`silence ${v}`)}
          />
          <Switch
            label="Press Enter after dictation"
            checked={s.enter === 'yes'}
            disabled={!local}
            onChange={(on) => void run(`enter ${on ? 'on' : 'off'}`)}
          />
          <Switch
            label="Hands-free conversation"
            checked={s.conversation === 'armed'}
            disabled={!local}
            onChange={(on) => void run(`conversation ${on ? 'on' : 'off'}`)}
          />
          <Switch
            label="Pause other audio while dictating"
            checked={s.pause === 'yes'}
            disabled={!local}
            onChange={(on) => void run(`pause ${on ? 'on' : 'off'}`)}
          />
          <Select
            label="Talk key"
            value={s.key}
            disabled={!local}
            options={[
              { value: 'f18', label: 'F18 (use with Karabiner)' },
              { value: 'cmd_r', label: 'Right Command' },
              { value: 'alt_r', label: 'Right Option' },
              { value: 'ctrl_r', label: 'Right Control' }
            ]}
            onChange={(v) => void chooseKey(v)}
          />
          <Button disabled={!local} onPress={() => void run('listen')}>Start dictation</Button>
        </Stack>
      </Card>

      <Card title="Agent voice" description="How replies are read to you">
        <Stack gap="md">
          <Switch
            label="Short spoken-style replies from agents"
            checked={s.hook}
            onChange={(on) => void plugin.request('voice.hook', { enable: on }).then((v) => set(state, v))}
          />
          <Switch label="Spoken replies" checked={!muted} onChange={(on) => void run(on ? 'unmute' : 'mute')} />
          <Select label="Voice" value={s.speaker} options={VOICES} onChange={(v) => void run(`speaker ${v}`)} />
          <Select label="Voice speed" value={speed} options={SPEEDS} onChange={(v) => void run(`speed ${v}`)} />
          <Button onPress={() => void run('stop')}>Shut up</Button>
        </Stack>
      </Card>

      <Button variant="ghost" onPress={() => void checkPermissions()}>Check permissions</Button>
      {read(doctor) ? <Code block>{read(doctor)}</Code> : null}
      <Text muted size="xs">Connected session: {s.following}</Text>
    </Stack>
  )
}

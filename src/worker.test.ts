import { assert, test } from 'orca/testing'
import { parse } from './worker'

test('parses the helper status text', () => {
  const s = parse('mode: local\nspeech-to-text: parakeet\nvoice: on\ntalk key: f18\nvoice speed: 1.15x\nmicrophone: default\n' +
    'hands-free conversation: armed\nstop double-tap recording after silence: 3.0 s\npress Enter after local dictation: yes\nfollowing: a: b')
  assert.equal(s.up, true)
  assert.equal(s.mode, 'local')
  assert.equal(s.speed, '1.15')
  assert.equal(s.key, 'f18')
  assert.equal(s.conversation, 'armed')
  assert.equal(s.silence, '3.0 s')
})

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

test('adds and removes only its own hook entry, with a backup', async () => {
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'voice-mode-')), 'settings.json')
  process.env.VOICE_MODE_CLAUDE_SETTINGS = file
  const other = { type: 'command', command: '/some/other/hook.sh' }
  fs.writeFileSync(file, JSON.stringify({ theme: 'dark', hooks: { UserPromptSubmit: [{ hooks: [other] }], Stop: [{ hooks: [other] }] } }))
  const mod = await import('./worker')
  assert.equal(mod.hookInstalled(), false)
  mod.setHook(true)
  assert.equal(mod.hookInstalled(), true)
  mod.setHook(true) // a second enable changes nothing
  let saved = JSON.parse(fs.readFileSync(file, 'utf8'))
  assert.equal(saved.hooks.UserPromptSubmit.length, 2)
  assert.equal(saved.theme, 'dark')
  mod.setHook(false)
  saved = JSON.parse(fs.readFileSync(file, 'utf8'))
  assert.deepEqual(saved.hooks.UserPromptSubmit, [{ hooks: [other] }])
  assert.deepEqual(saved.hooks.Stop, [{ hooks: [other] }])
  assert.equal(fs.readdirSync(path.dirname(file)).filter((f) => f.includes('.bak-voice-mode-')).length, 2)
})

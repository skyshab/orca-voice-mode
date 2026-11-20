import { assert, mockHost, mount, test } from 'orca/testing'
import * as panel from './panel'
import type { VoiceStatus } from './protocol'

const base: VoiceStatus = {
  up: true, phase: '', needsSetup: false, note: '', hook: true, inputVolume: 80, inputDevice: 'AirPods Max', key: 'f18',
  mode: 'local', stt: 'parakeet', voice: 'on', speed: '1.15', speaker: 'af_bella', mic: 'default', conversation: 'off',
  silence: '3.0 s', enter: 'yes', pause: 'yes', following: 'a: b'
}

test('moving the microphone volume slider sends the final value to the worker', async () => {
  const sent: number[] = []
  let current = 80
  const host = mockHost({
    requests: {
      'voice.status': () => ({ ...base, inputVolume: current }),
      'voice.inputVolume': ({ value }) => {
        sent.push(value)
        current = value
        return { ...base, inputVolume: current }
      }
    }
  })
  const view = await mount(panel, { host })
  await view.slide({ label: 'Microphone volume' }, 55)
  await new Promise((r) => setTimeout(r, 300))  // the panel waits for the drag to settle
  assert.deepEqual(sent, [55])
  assert.deepEqual(view.errors, [])
  await view.unmount()
})

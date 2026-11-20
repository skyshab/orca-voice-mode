// The panel <-> worker contract, declared once: src/panel.tsx and src/worker.ts both read it.
export {}

/** What the voice helper reports, parsed from its `status` text. Empty strings when it is down. */
export type VoiceStatus = {
  up: boolean
  /** True until `voice setup` has built the Python environment. */
  needsSetup: boolean
  /** The last lines of the setup log, when setup failed or is running. */
  note: string
  /** Whether Claude Code's settings carry the voice hook (short spoken-style replies while voice is on). */
  hook: boolean
  /** The talk key: f18, cmd_r, alt_r or ctrl_r. */
  key: string
  /** Set while a start, stop or restart is under way. */
  phase: '' | 'starting' | 'stopping' | 'restarting' | 'setting up'
  mode: string
  stt: string
  voice: string
  speed: string
  speaker: string
  mic: string
  conversation: string
  silence: string
  enter: string
  pause: string
  following: string
}

declare module 'orca' {
  interface PluginRequests {
    /** Reads the helper's current state. */
    'voice.status': { output: VoiceStatus }
    /** Starts, stops or restarts the helper process. Answers at once; the state follows through `voice.changed`. */
    'voice.helper': { input: { action: 'start' | 'stop' | 'restart' }; output: VoiceStatus }
    /** Adds or removes the voice hook in Claude Code's user settings (the file is backed up first). */
    'voice.hook': { input: { enable: boolean }; output: VoiceStatus }
    /** The helper's own check of what macOS and the machine allow (it must be running). */
    'voice.doctor': { output: { text: string } }
    /** Runs the one-time setup (installs tools, builds the Python environment, downloads models). */
    'voice.setup': { output: VoiceStatus }
    /** Sends one helper command, such as `mode local`, and answers the new state. */
    'voice.run': { input: { command: string }; output: VoiceStatus }
  }
  interface PluginPanelEvents {
    /** Posted to every open panel when the helper's state changes. */
    'voice.changed': VoiceStatus
  }
}

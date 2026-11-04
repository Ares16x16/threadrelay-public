// Compile-time regressions complement, never replace, shared runtime fixture tests.
import type { Command, SessionSnapshot, TopicResult } from './contracts';

// @ts-expect-error A generic process bridge must not become a valid Command.
const unknownCommand: Command = { version: 1, id: 'test', type: 'LaunchProcess', payload: {} };
// @ts-expect-error Commands cannot carry renderer-supplied handles.
const rawHandle: Command = { version: 1, id: 'test', type: 'Stop', payload: {}, process_handle: 42 };
// @ts-expect-error Classifier output cannot select destinations.
const destination: TopicResult = { topic_id: 'test', match: true, abstained: false, destination_id: 'test' };
// @ts-expect-error Attention is explicitly nullable, not silently omitted.
const missingNull: SessionSnapshot = { state: 'stopped', counters: { observed: 0, matched: 0, queued: 0, sent: 0 } };
void [unknownCommand, rawHandle, destination, missingNull];

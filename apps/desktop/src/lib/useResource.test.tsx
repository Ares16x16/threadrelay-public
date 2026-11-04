import { act, renderHook, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { useResource } from './useResource';

it('keeps a projection during observation and rejects its reply after a newer explicit read', async () => {
  let oldReply!:(value:number)=>void, newReply!:(value:number)=>void;
  const load=vi.fn().mockResolvedValueOnce(1)
    .mockImplementationOnce(()=>new Promise<number>(resolve=>{oldReply=resolve;}))
    .mockImplementationOnce(()=>new Promise<number>(resolve=>{newReply=resolve;}));
  const {result}=renderHook(()=>useResource<number>(load));
  await waitFor(()=>expect(result.current.value).toBe(1));
  act(()=>{void result.current.refresh({background:true});});
  expect(result.current.value).toBe(1);
  expect(result.current.loading).toBe(false);
  act(()=>{void result.current.refresh();});
  expect(result.current.loading).toBe(true);
  await act(async()=>newReply(3));
  await act(async()=>oldReply(2));
  expect(result.current.value).toBe(3);
  expect(result.current.loading).toBe(false);
});

it('surfaces a failed background read without clearing its error until a successful read', async () => {
  const load=vi.fn().mockResolvedValueOnce(1).mockRejectedValueOnce(new Error('private failure')).mockResolvedValueOnce(2);
  const {result}=renderHook(()=>useResource<number>(load));
  await waitFor(()=>expect(result.current.value).toBe(1));
  await act(async()=>{await result.current.refresh({background:true});});
  expect(result.current.error).toMatch(/Could not load current data/);
  expect(result.current.error).not.toContain('private failure');
  expect(result.current.value).toBe(1);
  await act(async()=>{await result.current.refresh();});
  expect(result.current.error).toBe('');
  expect(result.current.value).toBe(2);
});

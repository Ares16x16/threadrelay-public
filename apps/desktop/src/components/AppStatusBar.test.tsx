import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import type { AppStatus, SessionSnapshot } from '../lib/contracts';
import { AppStatusBar } from './AppStatusBar';
const session={state:'stopped' as const,counters:{observed:0,matched:0,queued:0,sent:0},attention_reason:null};
const value:AppStatus={service:'ready',model:{state:'not_loaded',model_id:'local-test-model'},activity:null,session};
it('separates ready app service from an unloaded on-demand model',async()=>{
 const worker={queryAppStatus:vi.fn().mockResolvedValue(value),initialize:vi.fn()};
 render(<AppStatusBar worker={worker}/>);
 expect(await screen.findByText('App: Ready')).toBeVisible();
 expect(screen.getByText('Model: Not loaded')).toBeVisible();
 expect(worker.initialize).not.toHaveBeenCalled();
});
it('shows observed loading and classification rather than inferring from workspace state',async()=>{
 const worker={queryAppStatus:vi.fn().mockResolvedValue({...value,model:{...value.model,state:'starting'},activity:'collection'})};
 const{rerender}=render(<AppStatusBar worker={worker}/>);
 expect(await screen.findByText('Model: Loading…')).toBeVisible();
 rerender(<AppStatusBar worker={worker} samplePending/>);
 expect(screen.getByText('Model: Processing sample…')).toBeVisible();
});
it('offers explicit app reconnect without starting a model or collection',async()=>{
 const worker={queryAppStatus:vi.fn().mockRejectedValue(new Error('unavailable')),initialize:vi.fn().mockResolvedValue(session)};
 const onReconnect=vi.fn();render(<AppStatusBar worker={worker} onReconnect={onReconnect}/>);
 fireEvent.click(await screen.findByRole('button',{name:'Reconnect app'}));
 await waitFor(()=>expect(worker.initialize).toHaveBeenCalledTimes(1));
 expect(onReconnect).toHaveBeenCalledWith(session);
});
it('keeps reconnect available after a paused or attention session even while status reads work',async()=>{
 const worker={queryAppStatus:vi.fn().mockResolvedValue({...value,session:{...session,state:'needs_attention',attention_reason:'dependency_cleanup_unconfirmed'}}),initialize:vi.fn().mockResolvedValue(session)};
 render(<AppStatusBar worker={worker}/>);
 fireEvent.click(await screen.findByRole('button',{name:'Reconnect app'}));
 await waitFor(()=>expect(worker.initialize).toHaveBeenCalledTimes(1));
});
it('does not suggest reconnecting a healthy app for unsupported feed content',async()=>{
 const worker={queryAppStatus:vi.fn().mockResolvedValue({...value,session:{...session,state:'needs_attention',attention_reason:'unknown_layout'}}),initialize:vi.fn()};
 render(<AppStatusBar worker={worker}/>);
 expect(await screen.findByText('App: Ready')).toBeVisible();
 expect(screen.queryByRole('button',{name:'Reconnect app'})).not.toBeInTheDocument();
});
it('continues read-only observation through repeated failures and stops after unmount',async()=>{
 vi.useFakeTimers();
 try {
  const worker={queryAppStatus:vi.fn().mockRejectedValueOnce(new Error('not ready')).mockRejectedValueOnce(new Error('not ready')).mockRejectedValueOnce(new Error('not ready')).mockResolvedValue(value),initialize:vi.fn()};
  const {unmount}=render(<AppStatusBar worker={worker}/>);
  await act(async()=>{});
  expect(screen.getByText('App: Unavailable')).toBeVisible();
  for(let retry=0;retry<3;retry++)await act(async()=>vi.advanceTimersByTimeAsync(2500));
  expect(screen.getByText('App: Ready')).toBeVisible();
  expect(worker.initialize).not.toHaveBeenCalled();
  unmount();const reads=worker.queryAppStatus.mock.calls.length;
  await act(async()=>vi.advanceTimersByTimeAsync(10000));
  expect(worker.queryAppStatus).toHaveBeenCalledTimes(reads);
 } finally {vi.useRealTimers();}
});

it('offers global Pause Resume and Stop using only explicit actions',async()=>{
 let current:SessionSnapshot={...session,state:'running'};
 const worker={queryAppStatus:vi.fn(async()=>({...value,session:current})),pause:vi.fn(async()=>{current={...session,state:'paused'};return current;}),stop:vi.fn(async()=>{current={...session,state:'stopped'};return current;})};
 const onState=vi.fn(),onBeforeControl=vi.fn(),onResume=vi.fn(async()=>({...session,state:'starting' as const}));
 render(<AppStatusBar worker={worker} onState={onState} onBeforeControl={onBeforeControl} onResume={onResume}/>);
 await screen.findByText('Collection: Running');expect(worker.pause).not.toHaveBeenCalled();
 fireEvent.click(screen.getByRole('button',{name:'Pause collection'}));await screen.findByText('Collection: Paused');
 expect(worker.pause).toHaveBeenCalledOnce();expect(onBeforeControl).toHaveBeenCalledWith('pause');
 fireEvent.click(screen.getByRole('button',{name:'Resume collection'}));await waitFor(()=>expect(onResume).toHaveBeenCalledOnce());
 fireEvent.click(screen.getByRole('button',{name:'Stop collection'}));await waitFor(()=>expect(worker.stop).toHaveBeenCalledOnce());
 expect(onBeforeControl).toHaveBeenCalledWith('stop');expect(onState).toHaveBeenCalledWith(expect.objectContaining({state:'stopped'}));
});

it('keeps global Stop available during an unobserved startup and discards a late running status',async()=>{
 let finish!:(data:AppStatus)=>void;const onState=vi.fn();
 const worker={queryAppStatus:vi.fn().mockImplementationOnce(()=>new Promise<AppStatus>(resolve=>{finish=resolve;})).mockResolvedValue(value),stop:vi.fn().mockResolvedValue(session)};
 render(<AppStatusBar worker={worker} onState={onState}/>);
 fireEvent.click(screen.getByRole('button',{name:'Stop collection'}));await waitFor(()=>expect(worker.stop).toHaveBeenCalledOnce());
 await act(async()=>finish({...value,session:{...session,state:'running'}}));
 expect(onState.mock.calls.every(([snapshot])=>snapshot?.state!=='running')).toBe(true);
});

it('shows current recovery only for a running session and revokes display authority on failed reads',async()=>{
 vi.useFakeTimers();
 try {
  const onState=vi.fn();const worker={queryAppStatus:vi.fn().mockResolvedValueOnce({...value,session:{...session,state:'running'},recovery:{reason:'unknown_layout',attempt:2,retry_in_seconds:2}}).mockRejectedValueOnce(new Error('offline')).mockResolvedValue({...value,recovery:{reason:'unknown_layout',attempt:2,retry_in_seconds:2}}),pause:vi.fn(),stop:vi.fn()};
  render(<AppStatusBar worker={worker} onState={onState}/>);await act(async()=>{});
  expect(screen.getByText(/Recovering feed.*next check in 2s/)).toBeVisible();
  await act(async()=>vi.advanceTimersByTimeAsync(2500));expect(screen.queryByText(/Recovering feed/)).not.toBeInTheDocument();expect(onState).toHaveBeenLastCalledWith(null);
  expect(screen.getByRole('button',{name:'Pause collection'})).toBeDisabled();expect(screen.getByRole('button',{name:'Stop collection'})).toBeEnabled();
  await act(async()=>vi.advanceTimersByTimeAsync(2500));expect(screen.queryByText(/Recovering feed/)).not.toBeInTheDocument();
 } finally {vi.useRealTimers();}
});

it('does not promote a status read issued before collection preparation after preparation settles',async()=>{
 vi.useFakeTimers();
 try {
  let finish!:(data:AppStatus)=>void;const onState=vi.fn();
  const worker={queryAppStatus:vi.fn().mockImplementationOnce(()=>new Promise<AppStatus>(resolve=>{finish=resolve;})).mockResolvedValue({...value,session:{...session,state:'starting'}})};
  const{rerender}=render(<AppStatusBar worker={worker} onState={onState}/>);
  rerender(<AppStatusBar worker={worker} onState={onState} preparing/>);
  rerender(<AppStatusBar worker={worker} onState={onState}/>);
  await act(async()=>finish(value));expect(onState).not.toHaveBeenCalled();
  await act(async()=>vi.advanceTimersByTimeAsync(2500));expect(onState).toHaveBeenLastCalledWith(expect.objectContaining({state:'starting'}));
 } finally {vi.useRealTimers();}
});


it('uses collection-only controls so Telegram keeps its worker and does not suggest reconnect for idle',async()=>{
 let current:SessionSnapshot={...session,state:'running'};
 const worker={queryAppStatus:vi.fn(async()=>({...value,session:current})),pause:vi.fn(),stop:vi.fn(),suspendCollection:vi.fn(async()=>{current={...session,state:'paused'};return current;}),stopCollection:vi.fn(async()=>{current=session;return current;})};
 render(<AppStatusBar worker={worker}/>);
 await screen.findByText('Collection: Running');
 fireEvent.click(screen.getByRole('button',{name:'Pause collection'}));await screen.findByText('Collection: Paused');
 expect(worker.suspendCollection).toHaveBeenCalledOnce();expect(worker.pause).not.toHaveBeenCalled();
 fireEvent.click(screen.getByRole('button',{name:'Stop collection'}));await screen.findByText('Collection: Stopped');
 expect(worker.stopCollection).toHaveBeenCalledOnce();expect(worker.stop).not.toHaveBeenCalled();
 expect(screen.queryByRole('button',{name:'Reconnect app'})).not.toBeInTheDocument();
});
it('shows the actual active mode and selected Auto route in the status strip',async()=>{const worker={queryAppStatus:vi.fn().mockResolvedValue({...value,session:{...session,state:'running',config:{feed_url:'https://www.threads.com/',mode:'auto',route_ids:['news']}}})};render(<AppStatusBar worker={worker}/>);expect(await screen.findByText('Mode: Auto · news')).toBeVisible();expect(screen.getByTitle(/warm/i)).toBeVisible();});

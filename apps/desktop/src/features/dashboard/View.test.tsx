import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { DashboardView } from './View';

it('defaults to continuous collection and exposes bounded limits only when requested',async()=>{
 const stopped:SessionSnapshot={state:'stopped',counters:{observed:0,matched:0,queued:0,sent:0},attention_reason:null};
 const worker={queryState:vi.fn().mockResolvedValue(stopped),pause:vi.fn(),stop:vi.fn(),start:vi.fn().mockResolvedValue({...stopped,state:'starting'})};
 render(<DashboardView compact observeInitial navigate={vi.fn()} worker={worker}/>);
 await waitFor(()=>expect(screen.getByRole('button',{name:'Start collecting'})).toBeEnabled());
 fireEvent.click(screen.getByText(/Collection options/));expect(screen.getByLabelText('Keep collecting until I pause')).toBeChecked();
 expect(screen.queryByLabelText('Duration in seconds')).not.toBeInTheDocument();expect(screen.queryByLabelText('Maximum observed posts')).not.toBeInTheDocument();
 expect(screen.getByLabelText('No automatic delivery limit')).toBeChecked();
 expect(screen.queryByLabelText('Maximum automatic deliveries')).not.toBeInTheDocument();
 fireEvent.click(screen.getByLabelText('Keep collecting until I pause'));
 fireEvent.change(screen.getByLabelText('Duration in seconds'),{target:{value:'20'}});fireEvent.change(screen.getByLabelText('Maximum observed posts'),{target:{value:'2'}});
 fireEvent.click(screen.getByRole('button',{name:'Start collecting'}));await waitFor(()=>expect(worker.start).toHaveBeenCalledWith(expect.objectContaining({continuous:false,duration_seconds:20,max_posts:2})));
});

it('prepares the visible browser before explicitly starting collection',async()=>{
 const stopped={state:'stopped' as const,counters:{observed:0,matched:0,queued:0,sent:0},attention_reason:null};
 let reveal!:()=>void;const beforeStart=vi.fn(()=>new Promise<void>(resolve=>{reveal=resolve;}));
 const worker={queryState:vi.fn().mockResolvedValue(stopped),pause:vi.fn(),stop:vi.fn(),start:vi.fn().mockResolvedValue({...stopped,state:'starting' as const})};
 render(<DashboardView compact observeInitial beforeStart={beforeStart} navigate={vi.fn()} worker={worker}/>);
 await waitFor(()=>expect(screen.getByRole('button',{name:'Start collecting'})).toBeEnabled());
 fireEvent.click(screen.getByRole('button',{name:'Start collecting'}));
 expect(beforeStart).toHaveBeenCalledOnce();expect(worker.start).not.toHaveBeenCalled();
 await act(async()=>reveal());await waitFor(()=>expect(worker.start).toHaveBeenCalledOnce());
});
it('does not start after Stop cancels a still-opening browser',async()=>{
 const stopped={state:'stopped' as const,counters:{observed:0,matched:0,queued:0,sent:0},attention_reason:null};
 let reveal!:()=>void;const beforeStart=()=>new Promise<void>(resolve=>{reveal=resolve;});
 const worker={queryState:vi.fn().mockResolvedValue(stopped),pause:vi.fn(),stop:vi.fn().mockResolvedValue(stopped),start:vi.fn()};
 render(<DashboardView compact observeInitial beforeStart={beforeStart} navigate={vi.fn()} worker={worker}/>);
 await waitFor(()=>expect(screen.getByRole('button',{name:'Start collecting'})).toBeEnabled());
 fireEvent.click(screen.getByRole('button',{name:'Start collecting'}));fireEvent.click(screen.getByRole('button',{name:'Stop'}));
 await act(async()=>reveal());expect(worker.stop).toHaveBeenCalledOnce();expect(worker.start).not.toHaveBeenCalled();
});

it('keeps managed Threads controls compact with secondary actions inside options',async()=>{
 const stopped:SessionSnapshot={state:'stopped',counters:{observed:0,matched:0,queued:0,sent:0},attention_reason:null};
 render(<DashboardView compact connectionManaged initialSnapshot={stopped} navigate={vi.fn()} worker={{queryState:vi.fn().mockResolvedValue(stopped),pause:vi.fn(),stop:vi.fn(),start:vi.fn(),queryReadiness:vi.fn()}}><section aria-label="Browser fixture"/></DashboardView>);
 const start=await screen.findByRole('button',{name:'Start collecting'});
 const options=screen.getByText(/Collection options/).closest('details');
 expect(options).not.toHaveAttribute('open');
 expect(start.closest('details')).toBeNull();
 expect(screen.getByText('Refresh session').closest('details')).toBe(options);
 expect(screen.getByText('Check setup').closest('details')).toBe(options);
 expect(screen.getByText(/Choose Start collecting to capture/).closest('details')).toBe(options);
 expect(screen.getByRole('heading',{name:'Threads'}).parentElement?.parentElement).toHaveClass('threads-page-header');
 fireEvent.click(screen.getByText(/Collection options/));
 expect(screen.getByRole('button',{name:'Refresh session'})).toBeEnabled();
 expect(screen.getByRole('button',{name:'Check setup'})).toBeEnabled();
 expect(screen.getByRole('region',{name:'Browser fixture'})).toBeInTheDocument();
});

it('blocks managed Start when global Running arrives during an older local read',async()=>{
 const stopped:SessionSnapshot={state:'stopped',counters:{observed:0,matched:0,queued:0,sent:0},attention_reason:null};
 let resolve!:(state:SessionSnapshot)=>void;
 const worker={queryState:vi.fn(()=>new Promise<SessionSnapshot>(done=>{resolve=done;})),pause:vi.fn(),stop:vi.fn(),start:vi.fn()};
 const {rerender}=render(<DashboardView compact connectionManaged observeInitial initialSnapshot={stopped} navigate={vi.fn()} worker={worker}/>);
 await waitFor(()=>expect(worker.queryState).toHaveBeenCalledOnce());
 rerender(<DashboardView compact connectionManaged observeInitial initialSnapshot={{...stopped,state:'running'}} navigate={vi.fn()} worker={worker}/>);
 expect(screen.getByRole('button',{name:'Start collecting'})).toBeDisabled();
 await act(async()=>resolve(stopped));
 expect(screen.getByRole('button',{name:'Start collecting'})).toBeDisabled();
});

it('takes the managed dashboard to the visible Threads controls before collection',async()=>{
 const navigate=vi.fn(),start=vi.fn();
 render(<DashboardView connectionManaged observeInitial navigate={navigate} worker={{queryState:vi.fn().mockResolvedValue({state:'stopped',counters:{observed:0,matched:0,queued:0,sent:0},attention_reason:null}),pause:vi.fn(),stop:vi.fn(),start}}/>);
 fireEvent.click(await screen.findByRole('button',{name:'Collect in Threads'}));
 expect(navigate).toHaveBeenCalledWith('Threads');
 expect(start).not.toHaveBeenCalled();
 expect(screen.queryByLabelText('Feed URL')).not.toBeInTheDocument();
 expect(screen.queryByRole('button',{name:'Stop'})).not.toBeInTheDocument();
 expect(screen.queryByRole('button',{name:'Pause'})).not.toBeInTheDocument();
});

it('keeps limits and session explanations behind one closed disclosure on Dashboard',()=>{
 render(<DashboardView connectionManaged navigate={vi.fn()}/>);
 expect(screen.getByRole('button',{name:'Collect in Threads'})).toBeVisible();
 expect(screen.queryByText('Mode unconfirmed')).not.toBeInTheDocument();
 const details=screen.getByText('Session details and limits').closest('details');
 expect(details).not.toHaveAttribute('open');
 expect(screen.getByText(/Telegram can require a longer wait/).closest('details')).toBe(details);
 expect(screen.getByText(/No new sends start after Pause or Stop/).closest('details')).toBe(details);
 expect(screen.queryByRole('heading',{name:'Readiness'})).not.toBeInTheDocument();
});

it('starts collection with one explicit action without requiring a separate readiness click',async()=>{
 const stopped={state:'stopped' as const,counters:{observed:0,matched:0,queued:0,sent:0},attention_reason:null};
 const worker={initialize:vi.fn().mockResolvedValue(stopped),queryState:vi.fn(),pause:vi.fn(),stop:vi.fn(),queryReadiness:vi.fn(),start:vi.fn().mockResolvedValue({...stopped,state:'starting' as const})};
 render(<DashboardView navigate={vi.fn()} worker={worker}/>);
 fireEvent.click(screen.getByRole('button',{name:/^(Open local workspace|Reconnect app)$/}));
 await screen.findByText(/Last validated worker state/);
 const start=screen.getByRole('button',{name:/^(Start|Start collecting)$/});
 expect(start).toBeEnabled();
 fireEvent.click(start);
 await waitFor(()=>expect(worker.start).toHaveBeenCalledTimes(1));
 expect(worker.queryReadiness).not.toHaveBeenCalled();
});
import type { SessionSnapshot } from '../../lib/contracts';
it('observes background workspace startup without initializing or starting a session',async()=>{
 vi.useFakeTimers();
 try {
  const stopped:SessionSnapshot={state:'stopped',counters:{observed:0,matched:0,queued:0,sent:0},attention_reason:null};
  const worker={queryState:vi.fn().mockRejectedValueOnce(new Error('workspace opening')).mockResolvedValue(stopped),initialize:vi.fn(),start:vi.fn(),pause:vi.fn(),stop:vi.fn()};
  render(<DashboardView navigate={vi.fn()} worker={worker} observeInitial/>);
  await act(async()=>{});
  expect(worker.queryState).toHaveBeenCalledTimes(1);
  await act(async()=>{await vi.advanceTimersByTimeAsync(1000);});
  expect(screen.getByText(/Last validated worker state/)).toBeVisible();
  expect(worker.initialize).not.toHaveBeenCalled();expect(worker.start).not.toHaveBeenCalled();
  expect(screen.getByRole('button',{name:'Start collecting'})).toBeEnabled();
 } finally {vi.useRealTimers();}
});
it('does not restore authority automatically after an initially observed running session fails',async()=>{
 vi.useFakeTimers();
 try {
  const running:SessionSnapshot={state:'running',counters:{observed:0,matched:0,queued:0,sent:0},attention_reason:null};
  const worker={queryState:vi.fn().mockResolvedValueOnce(running).mockRejectedValue(new Error('worker lost')),initialize:vi.fn(),start:vi.fn(),pause:vi.fn(),stop:vi.fn()};
  render(<DashboardView navigate={vi.fn()} worker={worker} observeInitial/>);
  await act(async()=>{});
  expect(screen.getByText('Running',{exact:true})).toBeVisible();
  await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});
  expect(worker.queryState).toHaveBeenCalledTimes(2);
  expect(screen.getByRole('button',{name:'Pause'})).toBeDisabled();
  expect(worker.initialize).not.toHaveBeenCalled();expect(worker.start).not.toHaveBeenCalled();
 } finally {vi.useRealTimers();}
});
it('shows a stopped, unconfirmed initial session and proposed—not approved—limits',()=>{
 render(<DashboardView navigate={vi.fn()}/>);
 expect(screen.getByText('Stopped')).toBeVisible();
 expect(screen.getByRole('button',{name:'Start collecting'})).toBeDisabled();
 expect(screen.getByText(/Worker not connected/)).toBeVisible();
 fireEvent.click(screen.getByText('Session details and limits'));
 expect(screen.getByText(/Telegram can require a longer wait/)).toBeVisible();
});
it('updates only from a validated explicit query and allows a running session to pause', async()=>{
 const running={state:'running' as const,counters:{observed:3,matched:2,queued:1,sent:0},attention_reason:null};
 const paused={...running,state:'paused' as const};
 const worker={queryState:vi.fn().mockResolvedValue(running),pause:vi.fn().mockResolvedValue(paused),stop:vi.fn().mockResolvedValue(paused)};
 render(<DashboardView navigate={vi.fn()} worker={worker}/>);
 expect(worker.queryState).not.toHaveBeenCalled();
 fireEvent.click(screen.getByRole('button',{name:'Refresh session'}));
 expect(await screen.findByText('Running')).toBeVisible();
 fireEvent.click(screen.getByRole('button',{name:'Pause'}));
 expect(await screen.findByText('Paused')).toBeVisible();
});
it('handles successful and failed refresh under the real StrictMode lifecycle', async()=>{
 const {StrictMode}=await import('react');
 const running={state:'running' as const,counters:{observed:1,matched:0,queued:0,sent:0},attention_reason:null};
 const worker={queryState:vi.fn().mockResolvedValueOnce(running).mockRejectedValueOnce(new Error('offline')),pause:vi.fn(),stop:vi.fn()};
 render(<StrictMode><DashboardView navigate={vi.fn()} worker={worker}/></StrictMode>);
 fireEvent.click(screen.getByRole('button',{name:'Refresh session'}));
 expect(await screen.findByText('Running')).toBeVisible();
 expect(screen.getByRole('button',{name:'Pause'})).toBeEnabled();
 fireEvent.click(screen.getByRole('button',{name:'Refresh session'}));
 expect(await screen.findByRole('alert')).toHaveTextContent('The app connection was lost');
 expect(screen.getByRole('button',{name:'Refresh session'})).toBeEnabled();
 expect(screen.getByRole('button',{name:'Pause'})).toBeDisabled();
});
it('opens local data only on an explicit click', async()=>{
 const stopped={state:'stopped' as const,counters:{observed:0,matched:0,queued:0,sent:0},attention_reason:null};
 const worker={initialize:vi.fn().mockResolvedValue(stopped),queryState:vi.fn(),pause:vi.fn(),stop:vi.fn()};
 render(<DashboardView navigate={vi.fn()} worker={worker}/>);
 expect(worker.initialize).not.toHaveBeenCalled();
 fireEvent.click(screen.getByRole('button',{name:'Reconnect app'}));
 expect(await screen.findByText(/Last validated worker state/)).toBeVisible();
 expect(worker.initialize).toHaveBeenCalledTimes(1);
});
it('does not present a made-up session mode or feed as current authority',()=>{
 render(<DashboardView navigate={vi.fn()}/>);
 expect(screen.queryByText('No feed selected.')).not.toBeInTheDocument();
 fireEvent.click(screen.getByText('Session details and limits'));
 expect(screen.getByText(/Mode and feed are unconfirmed/)).toBeVisible();
});
it('labels mode and feed from a validated session projection', async()=>{
 const running={state:'running' as const,counters:{observed:0,matched:0,queued:0,sent:0},attention_reason:null,config:{feed_url:'https://www.threads.com/',mode:'dry_run' as const,duration_seconds:30,max_posts:2,max_auto_deliveries:0}};
 const worker={queryState:vi.fn().mockResolvedValue(running),pause:vi.fn(),stop:vi.fn()};
 render(<DashboardView navigate={vi.fn()} worker={worker}/>);
 fireEvent.click(screen.getByRole('button',{name:'Refresh session'}));
 fireEvent.click(screen.getByText('Session details and limits'));
 expect(await screen.findByText(/dry run.*https:\/\/www.threads.com\//i)).toBeVisible();
});
it('checks mode-specific readiness before an explicit Start', async()=>{
 const stopped={state:'stopped' as const,counters:{observed:0,matched:0,queued:0,sent:0},attention_reason:null};
 const readiness={mode:'dry_run' as const,browser:'ready' as const,inference:'ready' as const,telegram:'unavailable' as const,automatic:'unavailable' as const,can_start:true,reason:null};
 const worker={initialize:vi.fn().mockResolvedValue(stopped),queryState:vi.fn(),pause:vi.fn(),stop:vi.fn(),queryReadiness:vi.fn().mockResolvedValue(readiness),start:vi.fn().mockResolvedValue({...stopped,state:'starting' as const})};
 render(<DashboardView navigate={vi.fn()} worker={worker}/>);
 fireEvent.click(screen.getByRole('button',{name:'Reconnect app'}));
 await screen.findByText(/Last validated worker state/);
 fireEvent.change(screen.getByLabelText('Session mode'),{target:{value:'dry_run'}});
 fireEvent.click(screen.getByRole('button',{name:'Check setup'}));
 expect(await screen.findByText(/Telegram delivery: not enabled/)).toBeVisible();
 fireEvent.click(screen.getByRole('button',{name:'Start collecting'}));
 expect(worker.start).toHaveBeenCalledWith(expect.objectContaining({mode:'dry_run'}));
});
it('allows closing an initialized, stopped local workspace', async()=>{
 const stopped={state:'stopped' as const,counters:{observed:0,matched:0,queued:0,sent:0},attention_reason:null};
 const worker={initialize:vi.fn().mockResolvedValue(stopped),queryState:vi.fn(),pause:vi.fn(),stop:vi.fn().mockResolvedValue(stopped)};
 render(<DashboardView navigate={vi.fn()} worker={worker}/>);
 fireEvent.click(screen.getByRole('button',{name:'Reconnect app'}));
 await screen.findByText(/Last validated worker state/);
 expect(screen.getByRole('button',{name:'Stop'})).toBeEnabled();
 fireEvent.click(screen.getByRole('button',{name:'Stop'}));
 expect(worker.stop).toHaveBeenCalledTimes(1);
});
it('marks worker authority unconfirmed after readiness connection failure', async()=>{
 const stopped={state:'stopped' as const,counters:{observed:0,matched:0,queued:0,sent:0},attention_reason:null};
 const worker={initialize:vi.fn().mockResolvedValue(stopped),queryState:vi.fn(),pause:vi.fn(),stop:vi.fn(),queryReadiness:vi.fn().mockRejectedValue(new Error('offline'))};
 render(<DashboardView navigate={vi.fn()} worker={worker}/>);
 fireEvent.click(screen.getByRole('button',{name:'Reconnect app'}));
 await screen.findByText(/Last validated worker state/);
 fireEvent.click(screen.getByRole('button',{name:'Check setup'}));
 await screen.findByRole('alert');
 expect(screen.getByRole('button',{name:'Stop'})).toBeDisabled();
 expect(screen.getByText(/initial state is unconfirmed/)).toBeVisible();
});
it('revokes displayed running authority after a background state check fails without reopening', async()=>{
 const running={state:'running' as const,counters:{observed:0,matched:0,queued:0,sent:0},attention_reason:null};
 const worker={initialize:vi.fn(),queryState:vi.fn().mockResolvedValueOnce(running).mockRejectedValue(new Error('revoked')),pause:vi.fn(),stop:vi.fn()};
 render(<DashboardView navigate={vi.fn()} worker={worker}/>);
 fireEvent.click(screen.getByRole('button',{name:'Refresh session'}));
 expect(await screen.findByText('Running')).toBeVisible();
 expect(await screen.findByRole('alert',{}, {timeout:2500})).toHaveTextContent('Use Reconnect app');
 expect(screen.getByRole('button',{name:'Pause'})).toBeDisabled();
 expect(worker.initialize).not.toHaveBeenCalled();
});
it('keeps Stop usable during preparation and ignores a late initialized running reply', async()=>{
 const {act}=await import('@testing-library/react');
 let finish!:(value:SessionSnapshot)=>void;
 const stopped={state:'stopped' as const,counters:{observed:0,matched:0,queued:0,sent:0},attention_reason:null};
 const worker={initialize:vi.fn(()=>new Promise<SessionSnapshot>(resolve=>{finish=resolve;})),queryState:vi.fn(),pause:vi.fn(),stop:vi.fn().mockResolvedValue({...stopped,state:'stopping'})};
 render(<DashboardView navigate={vi.fn()} worker={worker}/>);
 fireEvent.click(screen.getByRole('button',{name:'Reconnect app'}));
 expect(screen.getByRole('button',{name:'Stop'})).toBeEnabled();
 fireEvent.click(screen.getByRole('button',{name:'Stop'}));
 expect(worker.stop).toHaveBeenCalledTimes(1);
 await act(async()=>{finish({...stopped,state:'running'});});
 expect(screen.queryByText('Running',{exact:true})).not.toBeInTheDocument();
 expect(screen.getByRole('button',{name:'Start collecting'})).toBeDisabled();
});

it('cancels a pending Start without accepting its late state as authority', async()=>{
 const {act}=await import('@testing-library/react');
 let finish!:(value:SessionSnapshot)=>void;
 const stopped:SessionSnapshot={state:'stopped',counters:{observed:0,matched:0,queued:0,sent:0},attention_reason:null};
 const worker={initialize:vi.fn().mockResolvedValue(stopped),queryState:vi.fn(),pause:vi.fn(),stop:vi.fn().mockResolvedValue({...stopped,state:'stopping'}),queryReadiness:vi.fn().mockResolvedValue({mode:'review',browser:'ready',inference:'ready',telegram:'ready',automatic:'unavailable',can_start:true,reason:null}),start:vi.fn(()=>new Promise<SessionSnapshot>(resolve=>{finish=resolve;}))};
 const onState=vi.fn();
 render(<DashboardView navigate={vi.fn()} worker={worker} onState={onState}/>);
 fireEvent.click(screen.getByRole('button',{name:'Reconnect app'}));
 await screen.findByText(/Last validated worker state/);
 fireEvent.click(screen.getByRole('button',{name:'Check setup'}));
 await screen.findByText(/Ready for this mode/);
 fireEvent.click(screen.getByRole('button',{name:'Start collecting'}));
 expect(screen.getByRole('button',{name:'Stop'})).toBeEnabled();
 fireEvent.click(screen.getByRole('button',{name:'Stop'}));
 await act(async()=>{finish({...stopped,state:'starting'});});
 expect(screen.queryByText('Starting',{exact:true})).not.toBeInTheDocument();
 expect(onState).toHaveBeenLastCalledWith(null);
 expect(screen.getByRole('button',{name:'Start collecting'})).toBeDisabled();
});

it.each([false,true])('shows and clears the reported collection reason in compact=%s controls',async(compact)=>{
 const stopped:SessionSnapshot={state:'stopped',counters:{observed:0,matched:0,queued:0,sent:0},attention_reason:null};
 const worker={queryState:vi.fn().mockResolvedValueOnce({...stopped,state:'needs_attention',attention_reason:'preparation_failed'}).mockResolvedValue(stopped),pause:vi.fn(),stop:vi.fn(),start:vi.fn()};
 render(<DashboardView compact={compact} navigate={vi.fn()} worker={worker}/>);
 fireEvent.click(screen.getByRole('button',{name:'Refresh session'}));
 const reason=await screen.findByRole('alert');
 expect(reason).toHaveTextContent('Could not prepare the Threads view or local model.');
 expect(reason).toHaveTextContent('preparation_failed');
 expect(worker.start).not.toHaveBeenCalled();
 fireEvent.click(screen.getByRole('button',{name:'Refresh session'}));
 await act(async()=>{});
 expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

it.each([false,true])('shows unknown reasons as escaped last-known text in compact=%s controls',compact=>{
 const reason='new_native_reason <img src=x>';
 const snapshot:SessionSnapshot={state:'needs_attention',counters:{observed:0,matched:0,queued:0,sent:0},attention_reason:reason};
 render(<DashboardView compact={compact} initialSnapshot={snapshot} navigate={vi.fn()}/>);
 expect(screen.getByRole('alert')).toHaveTextContent(reason);
 expect(screen.getByRole('alert')).toHaveTextContent('Last reported:');
 expect(screen.queryByRole('img')).not.toBeInTheDocument();
});

it('requires one enabled Auto route and sends its id when starting Auto collection',async()=>{
 const stopped={state:'stopped' as const,counters:{observed:0,matched:0,queued:0,sent:0},attention_reason:null};
 const worker={queryState:vi.fn().mockResolvedValue(stopped),pause:vi.fn(),stop:vi.fn(),start:vi.fn().mockResolvedValue({...stopped,state:'starting' as const}),queryRoutes:vi.fn().mockResolvedValue([{id:'news',version:1,topic_ids:['t'],destination_id:'-456',mode:'auto',enabled:true,account_id:'123',account_epoch:1,sender:{kind:'user',id:'123'}}])};
 render(<DashboardView compact observeInitial navigate={vi.fn()} worker={worker}/>);await waitFor(()=>expect(screen.getByRole('button',{name:'Start collecting'})).toBeEnabled());fireEvent.click(screen.getByText(/Collection options/));fireEvent.change(screen.getByLabelText('Session mode'),{target:{value:'auto'}});expect(screen.getByRole('button',{name:'Start collecting'})).toBeDisabled();fireEvent.change(await screen.findByLabelText('Auto route'),{target:{value:'news'}});fireEvent.click(screen.getByRole('button',{name:'Start collecting'}));await waitFor(()=>expect(worker.start).toHaveBeenCalledWith(expect.objectContaining({mode:'auto',route_ids:['news']})));
});
it('shows the worker mode and locks options during an active managed session, while preserving paused edits across polls',async()=>{
 const stopped:SessionSnapshot={state:'stopped',counters:{observed:0,matched:0,queued:0,sent:0},attention_reason:null};const running:SessionSnapshot={...stopped,state:'running',config:{feed_url:'https://www.threads.com/',mode:'auto',route_ids:['news']}};const worker={queryState:vi.fn(),pause:vi.fn(),stop:vi.fn(),start:vi.fn(),queryRoutes:vi.fn().mockResolvedValue([{id:'news',version:1,topic_ids:['t'],destination_id:'-456',mode:'auto',enabled:true,account_id:'123',account_epoch:1,sender:{kind:'user',id:'123'}}])};
 const {rerender}=render(<DashboardView compact connectionManaged initialSnapshot={stopped} navigate={vi.fn()} worker={worker}/>);fireEvent.click(screen.getByText(/Collection options/));fireEvent.change(screen.getByLabelText('Feed URL'),{target:{value:'https://example.test/'}});rerender(<DashboardView compact connectionManaged initialSnapshot={running} navigate={vi.fn()} worker={worker}/>);expect(await screen.findByText('Mode: Auto · news')).toBeVisible();expect(screen.getByLabelText('Session mode')).toHaveValue('auto');expect(screen.getByLabelText('Session mode')).toBeDisabled();expect(screen.getByText(/Pause collection to change options/)).toBeVisible();
 const paused={...running,state:'paused' as const};rerender(<DashboardView compact connectionManaged initialSnapshot={paused} navigate={vi.fn()} worker={worker}/>);await waitFor(()=>expect(screen.getByLabelText('Session mode')).toBeEnabled());fireEvent.change(screen.getByLabelText('Feed URL'),{target:{value:'https://example.test/'}});rerender(<DashboardView compact connectionManaged initialSnapshot={{...paused}} navigate={vi.fn()} worker={worker}/>);expect(screen.getByLabelText('Feed URL')).toHaveValue('https://example.test/');
});

it('supports explicit unlimited delivery and a larger finite budget in the Start payload',async()=>{
 const stopped:SessionSnapshot={state:'stopped',counters:{observed:0,matched:0,queued:0,sent:0},attention_reason:null};
 const route={id:'route-fast',version:1,topic_ids:['science'],mode:'auto' as const,enabled:true,account_id:'123',account_epoch:0,destination_id:'-20',sender:{kind:'user' as const,id:'123'}};
 const worker={queryState:vi.fn().mockResolvedValue(stopped),queryRoutes:vi.fn().mockResolvedValue([route]),pause:vi.fn(),stop:vi.fn(),start:vi.fn().mockResolvedValue({...stopped,state:'starting'})};
 render(<DashboardView compact observeInitial navigate={vi.fn()} worker={worker}/>);
 await waitFor(()=>expect(screen.getByRole('button',{name:'Start collecting'})).toBeEnabled());
 fireEvent.click(screen.getByText(/Collection options/));
 expect(screen.getByLabelText('No automatic delivery limit')).toBeChecked();
 expect(screen.queryByLabelText('Maximum automatic deliveries')).not.toBeInTheDocument();
 fireEvent.change(screen.getByLabelText('Session mode'),{target:{value:'auto'}});
 await screen.findByRole('option',{name:'route-fast'});fireEvent.change(screen.getByLabelText('Auto route'),{target:{value:'route-fast'}});
 fireEvent.click(screen.getByLabelText('No automatic delivery limit'));
 fireEvent.change(screen.getByLabelText('Maximum automatic deliveries'),{target:{value:'250'}});
 fireEvent.click(screen.getByRole('button',{name:'Start collecting'}));
 await waitFor(()=>expect(worker.start).toHaveBeenCalledWith(expect.objectContaining({max_auto_deliveries:250,unlimited_auto_deliveries:false,route_ids:['route-fast']})));
});

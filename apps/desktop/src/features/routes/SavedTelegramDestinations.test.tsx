import {act,fireEvent,render,screen,waitFor,within} from '@testing-library/react';
import {expect,it,vi} from 'vitest';
import {SavedTelegramDestinationsPane,SavedDestinationPicker} from './SavedTelegramDestinations';
const account={account_id:'7',epoch:2,auth_state:'ready' as const,display_name:'Fixture',network_state:'online' as const,revocation_pending:false};
const candidate={chat_id:'-20',title:'Chosen group',kind:'group' as const,can_verify:true,sender:{kind:'user' as const,id:'7'}};
const saved={chat_id:'-20',title:'Chosen group',kind:'group' as const,sender:{kind:'user' as const,id:'7'},account_id:'7',account_epoch:2,saved_at:'2026-11-01T12:00:00Z'};
function worker(){return {querySavedTelegramDestinations:vi.fn().mockResolvedValue({items:[]}),saveTelegramDestination:vi.fn().mockResolvedValue(undefined),queryTelegramDestinations:vi.fn().mockResolvedValue({state:'ready',account_id:'7',account_epoch:2,items:[candidate],next_cursor:null,can_load_more:false,exhausted:true,capped:false,blocked_reason:null}),refreshTelegramDestinations:vi.fn(),verifyDestination:vi.fn().mockResolvedValue(undefined),queryDestination:vi.fn().mockResolvedValue({state:'verified',snapshot:{account_id:'7',chat_id:'-20',title:'Chosen group',audience:'shared',permissions:{can_send:true,can_select_sender:false},sender:{kind:'user',id:'7'},paid_required:false}})};}
it('verifies and saves the explicitly selected chat with one action',async()=>{
 const w=worker(),onSaved=vi.fn();render(<SavedTelegramDestinationsPane worker={w} account={account} authoritative onSaved={onSaved}/>);
 fireEvent.click(await screen.findByRole('button',{name:'Choose Chosen group · -20'}));
 expect(screen.getByText(/Selected chat: Chosen group · -20/)).toBeVisible();
 expect(w.verifyDestination).not.toHaveBeenCalled();expect(w.saveTelegramDestination).not.toHaveBeenCalled();expect(w.refreshTelegramDestinations).not.toHaveBeenCalled();
 expect(screen.queryByRole('button',{name:'Verify selected chat'})).not.toBeInTheDocument();
 expect(screen.queryByRole('button',{name:'Check selected chat'})).not.toBeInTheDocument();
 const save=screen.getByRole('button',{name:'Save selected chat'});expect(save).toBeEnabled();
 w.querySavedTelegramDestinations.mockResolvedValue({items:[saved]});fireEvent.click(save);fireEvent.click(save);
 await waitFor(()=>expect(w.verifyDestination).toHaveBeenCalledExactlyOnceWith({chat_id:'-20',kind:'group',sender:{kind:'user',id:'7'}}));
 await waitFor(()=>expect(w.saveTelegramDestination).toHaveBeenCalledExactlyOnceWith({chat_id:'-20',account_id:'7',account_epoch:2}));
 await waitFor(()=>expect(onSaved).toHaveBeenCalledOnce());expect(await screen.findByText(/Saved: Chosen group · -20/)).toBeVisible();
 expect(screen.getByRole('status')).toHaveTextContent('Chat saved.');
});
it.each(['foreign','account','sender','paid','permission'])('refuses mismatched or unsafe verification (%s)',async(change)=>{
 const w=worker();const result=await w.queryDestination();result.snapshot={...result.snapshot,...(change==='foreign'?{chat_id:'-99'}:change==='account'?{account_id:'99'}:change==='sender'?{sender:{kind:'user',id:'99'}}:change==='paid'?{paid_required:true}:{permissions:{can_send:false,can_select_sender:false}})};w.queryDestination.mockResolvedValue(result);
 render(<SavedTelegramDestinationsPane worker={w} account={account} authoritative/>);fireEvent.click(await screen.findByRole('button',{name:/Choose Chosen group/}));fireEvent.click(screen.getByRole('button',{name:'Save selected chat'}));
 await screen.findByText(/Could not verify this chat/);expect(w.saveTelegramDestination).not.toHaveBeenCalled();
});
it('drops a late verified reply when the account epoch changes',async()=>{
 const w=worker();let finish!:(value:Awaited<ReturnType<typeof w.queryDestination>>)=>void;const result=await w.queryDestination();w.queryDestination.mockImplementation(()=>new Promise(resolve=>{finish=resolve;}));
 const ui=render(<SavedTelegramDestinationsPane worker={w} account={account} authoritative/>);fireEvent.click(await screen.findByRole('button',{name:/Choose Chosen group/}));fireEvent.click(screen.getByRole('button',{name:'Save selected chat'}));
 await waitFor(()=>expect(finish).toBeDefined());ui.rerender(<SavedTelegramDestinationsPane worker={w} account={{...account,epoch:3}} authoritative/>);await act(async()=>finish(result));
 expect(screen.getByRole('button',{name:'Save selected chat'})).toBeDisabled();expect(w.saveTelegramDestination).not.toHaveBeenCalled();
});
it('does not save after navigating away while verification is in flight',async()=>{
 const w=worker(),result=await w.queryDestination();let finish!:(value:unknown)=>void;
 w.queryDestination.mockImplementation(()=>new Promise(resolve=>{finish=resolve;}));
 const ui=render(<SavedTelegramDestinationsPane worker={w} account={account} authoritative/>);
 fireEvent.click(await screen.findByRole('button',{name:/Choose Chosen group/}));fireEvent.click(screen.getByRole('button',{name:'Save selected chat'}));
 await waitFor(()=>expect(finish).toBeDefined());ui.unmount();await act(async()=>finish(result));
 expect(w.saveTelegramDestination).not.toHaveBeenCalled();
});
it('reads pending verification automatically without another user click',async()=>{
 vi.useFakeTimers();
 try{
  const w=worker();const verified=await w.queryDestination();w.queryDestination.mockReset().mockResolvedValueOnce({state:'pending',snapshot:null}).mockResolvedValue(verified);
  await act(async()=>{render(<SavedTelegramDestinationsPane worker={w} account={account} authoritative/>);});
  fireEvent.click(screen.getByRole('button',{name:/Choose Chosen group/}));await act(async()=>{fireEvent.click(screen.getByRole('button',{name:'Save selected chat'}));});
  expect(screen.getByRole('status')).toHaveTextContent('Checking');expect(w.saveTelegramDestination).not.toHaveBeenCalled();
  await act(async()=>{await vi.advanceTimersByTimeAsync(500);});
  expect(w.queryDestination).toHaveBeenCalledTimes(2);expect(w.saveTelegramDestination).toHaveBeenCalledTimes(1);
 }finally{vi.useRealTimers();}
});
it('stops pending verification after its bounded wait without saving',async()=>{
 vi.useFakeTimers();
 try{
  const w=worker();w.queryDestination.mockResolvedValue({state:'pending',snapshot:null});
  await act(async()=>{render(<SavedTelegramDestinationsPane worker={w} account={account} authoritative/>);});
  fireEvent.click(screen.getByRole('button',{name:/Choose Chosen group/}));await act(async()=>{fireEvent.click(screen.getByRole('button',{name:'Save selected chat'}));});
  await act(async()=>{await vi.advanceTimersByTimeAsync(30000);});
  expect(screen.getByRole('status')).toHaveTextContent('timed out');expect(w.saveTelegramDestination).not.toHaveBeenCalled();
  const calls=w.queryDestination.mock.calls.length;await act(async()=>{await vi.advanceTimersByTimeAsync(30000);});expect(w.queryDestination).toHaveBeenCalledTimes(calls);
 }finally{vi.useRealTimers();}
});
it.each(['begin','read'])('times out a stalled verification %s and ignores its late success',async stage=>{
 vi.useFakeTimers();
 try{
  const w=worker(),result=await w.queryDestination();let finish!:(value?:unknown)=>void;
  if(stage==='begin')w.verifyDestination.mockImplementation(()=>new Promise(resolve=>{finish=resolve;}));
  else w.queryDestination.mockImplementation(()=>new Promise(resolve=>{finish=resolve;}));
  await act(async()=>{render(<SavedTelegramDestinationsPane worker={w} account={account} authoritative/>);});
  fireEvent.click(screen.getByRole('button',{name:/Choose Chosen group/}));await act(async()=>{fireEvent.click(screen.getByRole('button',{name:'Save selected chat'}));});
  await act(async()=>{await vi.advanceTimersByTimeAsync(30000);});
  expect(screen.getByRole('status')).toHaveTextContent('timed out');
  await act(async()=>finish(stage==='read'?result:undefined));
  expect(w.saveTelegramDestination).not.toHaveBeenCalled();
 }finally{vi.useRealTimers();}
});
it('accepts a lost save acknowledgement only after exact saved identity is read back',async()=>{
 const w=worker(),onSaved=vi.fn();w.saveTelegramDestination.mockRejectedValue(new Error('lost acknowledgement'));
 w.querySavedTelegramDestinations.mockResolvedValueOnce({items:[]}).mockResolvedValue({items:[saved]});
 render(<SavedTelegramDestinationsPane worker={w} account={account} authoritative onSaved={onSaved}/>);
 fireEvent.click(await screen.findByRole('button',{name:/Choose Chosen group/}));fireEvent.click(screen.getByRole('button',{name:'Save selected chat'}));
 await waitFor(()=>expect(onSaved).toHaveBeenCalledOnce());expect(screen.getByRole('status')).toHaveTextContent('Chat saved.');
 expect(w.saveTelegramDestination).toHaveBeenCalledTimes(1);expect(w.querySavedTelegramDestinations).toHaveBeenCalledTimes(2);
});
it.each(['account','epoch','chat','sender','kind'])('does not accept a foreign saved record after a lost acknowledgement (%s)',async change=>{
 const w=worker();w.saveTelegramDestination.mockRejectedValue(new Error('lost acknowledgement'));
 const foreign={...saved,...(change==='account'?{account_id:'99'}:change==='epoch'?{account_epoch:1}:change==='chat'?{chat_id:'-99'}:change==='kind'?{kind:'channel' as const}:{sender:{kind:'user' as const,id:'99'}})};
 w.querySavedTelegramDestinations.mockResolvedValueOnce({items:[]}).mockResolvedValue({items:[foreign]});
 render(<SavedTelegramDestinationsPane worker={w} account={account} authoritative/>);
 fireEvent.click(await screen.findByRole('button',{name:/Choose Chosen group/}));fireEvent.click(screen.getByRole('button',{name:'Save selected chat'}));
 await screen.findByText(/Saving the chat was not confirmed/);expect(w.saveTelegramDestination).toHaveBeenCalledTimes(1);expect(w.querySavedTelegramDestinations).toHaveBeenCalledTimes(2);
});
it('does not treat an old saved name as confirmation of a rejected save after a chat rename',async()=>{
 const w=worker(),onSaved=vi.fn(),result=await w.queryDestination();
 w.queryDestination.mockResolvedValue({...result,snapshot:{...result.snapshot,title:'Renamed group'}});
 w.saveTelegramDestination.mockRejectedValue(new Error('save rejected'));
 w.querySavedTelegramDestinations.mockResolvedValueOnce({items:[]}).mockResolvedValue({items:[saved]});
 render(<SavedTelegramDestinationsPane worker={w} account={account} authoritative onSaved={onSaved}/>);
 fireEvent.click(await screen.findByRole('button',{name:/Choose Chosen group/}));fireEvent.click(screen.getByRole('button',{name:'Save selected chat'}));
 await screen.findByText(/Saving the chat was not confirmed/);
 expect(onSaved).not.toHaveBeenCalled();expect(screen.getByRole('status')).not.toHaveTextContent('Chat saved.');
 expect(w.saveTelegramDestination).toHaveBeenCalledTimes(1);expect(w.querySavedTelegramDestinations).toHaveBeenCalledTimes(2);
});
it.each(['refresh','callback'])('keeps an acknowledged save successful when the %s fails',async failure=>{
 const w=worker(),onSaved=vi.fn(()=>{if(failure==='callback')throw new Error('view refresh failed');});
 if(failure==='refresh')w.querySavedTelegramDestinations.mockResolvedValueOnce({items:[]}).mockRejectedValue(new Error('read failed'));
 render(<SavedTelegramDestinationsPane worker={w} account={account} authoritative onSaved={onSaved}/>);
 fireEvent.click(await screen.findByRole('button',{name:/Choose Chosen group/}));fireEvent.click(screen.getByRole('button',{name:'Save selected chat'}));
 await waitFor(()=>expect(onSaved).toHaveBeenCalledOnce());expect(screen.getByRole('status')).toHaveTextContent('Chat saved.');
 expect(screen.queryByText(/Saving the chat was not confirmed/)).not.toBeInTheDocument();expect(w.saveTelegramDestination).toHaveBeenCalledTimes(1);
});
it('offers only current saved entries to a sending selector, without discovering chats',async()=>{
 const w=worker(),choose=vi.fn();w.querySavedTelegramDestinations.mockResolvedValue({items:[saved,{...saved,chat_id:'-30',title:'Old epoch',account_epoch:1}]});
 render(<SavedDestinationPicker worker={w} account={account} authoritative selectedId="" disabled={false} onChoose={choose}/>);
 fireEvent.click(await screen.findByRole('button',{name:'Choose saved Chosen group · -20'}));expect(choose).toHaveBeenCalledWith(saved);
 expect(screen.queryByText('Old epoch')).not.toBeInTheDocument();expect(screen.queryByRole('button',{name:'Refresh chats'})).not.toBeInTheDocument();expect(w.queryTelegramDestinations).not.toHaveBeenCalled();expect(w.verifyDestination).not.toHaveBeenCalled();
});
it('revokes a selected saved destination after a failed safe list refresh',async()=>{
 const w=worker(),choose=vi.fn();w.querySavedTelegramDestinations.mockResolvedValue({items:[saved]});
 render(<SavedDestinationPicker worker={w} account={account} authoritative selectedId="-20" disabled={false} onChoose={choose}/>);await screen.findByRole('button',{name:/Choose saved Chosen group/});choose.mockClear();w.querySavedTelegramDestinations.mockRejectedValue(new Error('offline'));
 fireEvent.click(screen.getByRole('button',{name:'Refresh saved chats'}));await screen.findByText(/Could not load current data/);expect(choose).toHaveBeenCalledWith(null);expect(w.refreshTelegramDestinations).not.toHaveBeenCalled();
});

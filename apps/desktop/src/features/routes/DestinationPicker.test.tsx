import {render,screen,fireEvent,waitFor,act} from '@testing-library/react';
import {expect,it,vi} from 'vitest';
import {DestinationPicker,type DestinationPage} from './DestinationPicker';
const account={account_id:'7',epoch:2,auth_state:'ready',network_state:'online',revocation_pending:false};
const own={chat_id:'7',title:'Saved Messages',kind:'saved_messages' as const,can_verify:true,sender:{kind:'user' as const,id:'7'}};
const group={chat_id:'-20',title:'中文朋友群',kind:'group' as const,can_verify:true,sender:{kind:'user' as const,id:'7'}};
function page(extra:Partial<DestinationPage>={}):DestinationPage{return {state:'ready',account_id:'7',account_epoch:2,items:[own,group],next_cursor:null,can_load_more:true,exhausted:false,capped:false,blocked_reason:null,...extra};}
function worker(){return {queryTelegramDestinations:vi.fn().mockResolvedValue(page()),refreshTelegramDestinations:vi.fn().mockResolvedValue(undefined)};}
it('shows names, chat kinds and IDs and chooses exact metadata without verifying or sending',async()=>{
 const w=worker(),choose=vi.fn();render(<DestinationPicker worker={w} account={account} authoritative selectedId="" disabled={false} onChoose={choose}/>);
 expect(await screen.findByRole('button',{name:/Choose 中文朋友群/})).toHaveTextContent('Group · -20');
 fireEvent.click(screen.getByRole('button',{name:/Choose 中文朋友群/}));expect(choose).toHaveBeenCalledWith(group);expect(w.refreshTelegramDestinations).not.toHaveBeenCalled();
 fireEvent.click(screen.getByRole('button',{name:'Refresh chats'}));await waitFor(()=>expect(w.refreshTelegramDestinations).toHaveBeenCalledWith({load_more:false}));
});
it('searches loaded names and pages locally, then explicitly loads more chat metadata',async()=>{
 const w=worker();w.queryTelegramDestinations.mockResolvedValue(page({next_cursor:'cursor2'}));render(<DestinationPicker worker={w} account={account} authoritative selectedId="" disabled={false} onChoose={()=>{}}/>);
 await screen.findByText('中文朋友群');fireEvent.change(screen.getByLabelText('Search loaded chats'),{target:{value:'朋友'}});
 await waitFor(()=>expect(w.queryTelegramDestinations).toHaveBeenLastCalledWith({cursor:null,limit:30,search:'朋友'}));
 fireEvent.click(screen.getByRole('button',{name:'Next chats page'}));await waitFor(()=>expect(w.queryTelegramDestinations).toHaveBeenLastCalledWith({cursor:'cursor2',limit:30,search:'朋友'}));
 fireEvent.click(screen.getByRole('button',{name:'Load more chats'}));await waitFor(()=>expect(w.refreshTelegramDestinations).toHaveBeenCalledWith({load_more:true}));
});
it('does not claim all chats when capped and does not offer unknown senders',async()=>{
 const w=worker();w.queryTelegramDestinations.mockResolvedValue(page({items:[{...group,can_verify:false,sender:null}],capped:true,can_load_more:false}));
 render(<DestinationPicker worker={w} account={account} authoritative selectedId="" disabled={false} onChoose={()=>{}}/>);
 expect(await screen.findByRole('button',{name:/Choose 中文朋友群/})).toBeDisabled();expect(screen.getByText(/chat limit was reached/i)).toBeVisible();expect(screen.queryByText('All available chats loaded.')).not.toBeInTheDocument();
});
it('rejects late old-account pages and does not refresh while offline',async()=>{
 const w=worker();let finish!:(v:DestinationPage)=>void;w.queryTelegramDestinations.mockImplementation(()=>new Promise(resolve=>{finish=resolve;}));
 const ui=render(<DestinationPicker worker={w} account={account} authoritative selectedId="" disabled={false} onChoose={()=>{}}/>);
 ui.rerender(<DestinationPicker worker={w} account={{...account,epoch:3,network_state:'offline'}} authoritative selectedId="" disabled={false} onChoose={()=>{}}/>);
 await act(async()=>finish(page()));expect(screen.queryByRole('button',{name:/Choose 中文朋友群/})).not.toBeInTheDocument();expect(screen.getByRole('button',{name:'Refresh chats'})).toBeDisabled();expect(w.refreshTelegramDestinations).not.toHaveBeenCalled();
});
it('explains only the static failed stage and reason without retrying network work',async()=>{
 vi.useFakeTimers();
 try {
  const w=worker();w.queryTelegramDestinations.mockResolvedValue(page({state:'failed',items:[],failure_stage:'load',failure_reason:'timeout',can_load_more:false}));
  await act(async()=>{render(<DestinationPicker worker={w} account={account} authoritative selectedId="" disabled={false} onChoose={()=>{}}/>);});
  expect(screen.getByText(/Step: loading chat lists\. No response arrived in time\./)).toBeVisible();
  await act(async()=>{await vi.advanceTimersByTimeAsync(10000);});
  expect(w.queryTelegramDestinations).toHaveBeenCalledTimes(1);expect(w.refreshTelegramDestinations).not.toHaveBeenCalled();
  expect(screen.getByRole('button',{name:'Refresh chats'})).toBeEnabled();
 } finally {vi.useRealTimers();}
});

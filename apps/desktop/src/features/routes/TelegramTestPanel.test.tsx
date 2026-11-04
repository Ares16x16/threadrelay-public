import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { AccountSnapshot, TelegramTestState } from '../../lib/contracts';
import { TelegramTestPanel } from './TelegramTestPanel';
const account:AccountSnapshot={account_id:'7',epoch:2,auth_state:'ready',display_name:'Fixture account',network_state:'online',revocation_pending:false};
const idle:TelegramTestState={state:'idle',chat_id:null,account_id:null,account_epoch:null,text:null,request_extra:null,temporary_message_id:null,final_message_id:null,reason:null};
const choice={chat_id:'-20',title:'Test',kind:'group' as const,account_id:'7',account_epoch:2,saved_at:'2026-11-01T12:00:00Z',sender:{kind:'user' as const,id:'7'}};
function worker(){return {queryTelegramTest:vi.fn().mockResolvedValue(idle),sendTelegramTest:vi.fn().mockResolvedValue(undefined),querySavedTelegramDestinations:vi.fn().mockResolvedValue({items:[choice,{...choice,chat_id:'-30',title:'Other group'}]}),queryTelegramDestinations:vi.fn().mockResolvedValue({state:'ready',account_id:'7',account_epoch:2,items:[choice,{...choice,chat_id:'-30',title:'Other group'}],next_cursor:null,can_load_more:false,exhausted:true,capped:false,blocked_reason:null}),refreshTelegramDestinations:vi.fn().mockResolvedValue(undefined)};}
afterEach(()=>vi.useRealTimers());

it('observes only local test state and loaded names on mount while login is unavailable',async()=>{
 const w=worker();render(<TelegramTestPanel worker={w} account={null} authoritative={false}/>);
 await waitFor(()=>expect(w.queryTelegramTest).toHaveBeenCalledOnce());
 expect(screen.getByRole('button',{name:'Send one test message'})).toBeDisabled();expect(screen.getByText(/Sign in.*Accounts/)).toBeVisible();
 expect(w.sendTelegramTest).not.toHaveBeenCalled();expect(w.refreshTelegramDestinations).not.toHaveBeenCalled();
});
it('rejects any selected destination except the exact Test group and refreshes chats only explicitly',async()=>{
 const w=worker();render(<TelegramTestPanel worker={w} account={account} authoritative/>);
 fireEvent.click(await screen.findByRole('button',{name:/Choose saved Test/}));expect(screen.getByRole('button',{name:'Send one test message'})).toBeEnabled();
 fireEvent.click(screen.getByRole('button',{name:/Choose saved Other group/}));expect(screen.getByRole('button',{name:'Send one test message'})).toBeDisabled();
 expect(screen.getByText(/Choose a group named exactly/)).toBeVisible();expect(w.sendTelegramTest).not.toHaveBeenCalled();
 fireEvent.click(screen.getByRole('button',{name:'Refresh saved chats'}));await waitFor(()=>expect(w.querySavedTelegramDestinations).toHaveBeenCalledTimes(2));expect(w.refreshTelegramDestinations).not.toHaveBeenCalled();expect(w.queryTelegramDestinations).not.toHaveBeenCalled();
});

it('never retries an uncertain command even when a later local status read says idle',async()=>{
 const w=worker();w.sendTelegramTest.mockRejectedValue(new Error('lost acknowledgment'));
 render(<TelegramTestPanel worker={w} account={account} authoritative/>);
 fireEvent.click(await screen.findByRole('button',{name:/Choose saved Test/}));fireEvent.click(screen.getByRole('button',{name:'Send one test message'}));
 expect(await screen.findByText(/The test request was not confirmed/)).toBeVisible();
 await waitFor(()=>expect(screen.getByRole('button',{name:'Read test status'})).toBeEnabled());
 fireEvent.click(screen.getByRole('button',{name:'Read test status'}));await act(async()=>{});
 expect(screen.getByRole('button',{name:'Send one test message'})).toBeDisabled();expect(w.sendTelegramTest).toHaveBeenCalledOnce();
});
it('sends one explicit harmless test bound to the chosen account and confirms only final sent state',async()=>{
 vi.useFakeTimers();const w=worker();let finish!:()=>void;
 w.sendTelegramTest.mockImplementation(()=>new Promise<void>(resolve=>{finish=resolve;}));
 render(<TelegramTestPanel worker={w} account={account} authoritative/>);await act(async()=>{});
 fireEvent.click(screen.getByRole('button',{name:/Choose saved Test/}));
 const panel=screen.getByRole('region',{name:'Test Telegram connection'});expect(within(panel).getByText(/Test · -20/)).toBeVisible();expect(within(panel).getByText(/Exactly one fixed test message/)).toBeVisible();
 fireEvent.click(screen.getByRole('button',{name:'Send one test message'}));fireEvent.click(screen.getByRole('button',{name:'Send one test message'}));
 expect(w.sendTelegramTest).toHaveBeenCalledOnce();expect(w.sendTelegramTest).toHaveBeenCalledWith({chat_id:'-20',account_id:'7',account_epoch:2});
 w.queryTelegramTest.mockResolvedValue({...idle,state:'sending',chat_id:'-20',account_id:'7',account_epoch:2,text:'ThreadRelay test: synthetic fixture',request_extra:'fixture-test',temporary_message_id:'-1'});
 await act(async()=>finish());expect(screen.queryByText(/Test confirmed by Telegram/)).not.toBeInTheDocument();
 await act(async()=>vi.advanceTimersByTimeAsync(1000));expect(screen.getByText(/Awaiting Telegram confirmation/)).toBeVisible();
 expect(screen.getByText('ThreadRelay test: synthetic fixture')).toBeVisible();
 w.queryTelegramTest.mockResolvedValue({...idle,state:'sent',chat_id:'-20',account_id:'7',account_epoch:2,text:'ThreadRelay test: synthetic fixture',final_message_id:'99'});
 await act(async()=>vi.advanceTimersByTimeAsync(1000));expect(screen.getByText(/Test confirmed by Telegram.*connection closed cleanly/)).toBeVisible();expect(screen.getByText(/Final message ID: 99/)).toBeVisible();
 expect(screen.getByRole('button',{name:'Send one test message'})).toBeDisabled();
 const reads=w.queryTelegramTest.mock.calls.length;await act(async()=>vi.advanceTimersByTimeAsync(3000));expect(w.queryTelegramTest).toHaveBeenCalledTimes(reads);
});
it('invalidates a chosen group when the account epoch changes',async()=>{
 const w=worker();const ui=render(<TelegramTestPanel worker={w} account={account} authoritative/>);
 fireEvent.click(await screen.findByRole('button',{name:/Choose saved Test/}));expect(screen.getByRole('button',{name:'Send one test message'})).toBeEnabled();
 ui.rerender(<TelegramTestPanel worker={w} account={{...account,epoch:3}} authoritative/>);
 expect(screen.getByRole('button',{name:'Send one test message'})).toBeDisabled();fireEvent.click(screen.getByRole('button',{name:'Send one test message'}));expect(w.sendTelegramTest).not.toHaveBeenCalled();
});
it.each(['failed','unknown'] as const)('never offers a blind send retry after %s',async state=>{
 const w=worker();w.queryTelegramTest.mockResolvedValue({...idle,state,chat_id:'-20',account_id:'7',account_epoch:2,reason:'test_outcome_unconfirmed'});
 render(<TelegramTestPanel worker={w} account={account} authoritative/>);
 expect(await screen.findByText(state==='unknown'?/Outcome unknown.*Do not resend/:/Test failed.*No retry was sent/)).toBeVisible();
 expect(screen.getByRole('button',{name:'Send one test message'})).toBeDisabled();expect(w.sendTelegramTest).not.toHaveBeenCalled();
});

it('never offers an unsaved discovered Test group as a test-send destination',async()=>{
 const w=worker();w.querySavedTelegramDestinations.mockResolvedValue({items:[]});
 render(<TelegramTestPanel worker={w} account={account} authoritative/>);
 await waitFor(()=>expect(w.querySavedTelegramDestinations).toHaveBeenCalledOnce());
 expect(screen.queryByRole('button',{name:/Choose.*Test/})).not.toBeInTheDocument();expect(screen.getByRole('button',{name:'Send one test message'})).toBeDisabled();
 expect(w.queryTelegramDestinations).not.toHaveBeenCalled();expect(w.sendTelegramTest).not.toHaveBeenCalled();
});

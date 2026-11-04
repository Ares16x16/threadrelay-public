import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import type { DeliveryJob, ManualHistoryPreview, ManualHistorySendState } from '../../lib/contracts';
import { ManualHistorySend } from './ManualHistorySend';

const job:DeliveryJob={id:'job1',version:3,kind:'job',draft_id:'draft1',draft_version:1,revision_id:'revision1',post_id:'post1',text:'Exact saved text',target:{account_id:'123',account_epoch:2,destination_id:'-456',destination_version:4,sender:{kind:'user',id:'123'}},due_at:null,timing:{mode:'now',timezone:'Asia/Hong_Kong'},status:'blocked',blocked_reasons:[],requires_approval:false,rule_id:null,delivery_id:null,attempt_id:null,created_at:'2026-11-01T00:00:00Z',updated_at:'2026-11-01T00:00:00Z',source_policy:'public_only'};
const preview=():ManualHistoryPreview=>({job_id:job.id,job_version:job.version,revision_id:job.revision_id!,text:job.text!,destination:{account_id:'123',chat_id:'-456',title:'Named recipient',audience:'public',sender:{kind:'user',id:'123'},permissions:{can_send:true,can_select_sender:false},paid_required:false},target:job.target!,preview_receipt:'a'.repeat(64),expires_at:new Date(Date.now()+60000).toISOString()});
const state=(value:ManualHistorySendState['state']):ManualHistorySendState=>({state:value,job_id:job.id,job_version:job.version,reason:null});
function worker(){return {previewManualHistorySend:vi.fn().mockResolvedValue(preview()),sendManualHistoryOnce:vi.fn().mockResolvedValue(undefined),queryManualHistorySend:vi.fn().mockResolvedValue(state('sent'))};}
async function confirm(){fireEvent.click(screen.getByRole('button',{name:'Send now'}));const dialog=await screen.findByRole('dialog',{name:'Send this message?'});return within(dialog).getByRole('button',{name:'Send this message'});}

it('allows cancelling the exact preview without sending or polling',async()=>{
 const w=worker(),busy=vi.fn();render(<ManualHistorySend worker={w} job={job} onBusy={busy}/>);await confirm();
 fireEvent.click(within(screen.getByRole('dialog')).getByRole('button',{name:'Cancel'}));expect(w.sendManualHistoryOnce).not.toHaveBeenCalled();expect(w.queryManualHistorySend).not.toHaveBeenCalled();expect(busy).toHaveBeenLastCalledWith(false);expect(screen.getByRole('button',{name:'Send now'})).toBeEnabled();
});
it('does not treat an acknowledgment as Sent and observes checking and sending before completion',async()=>{
 const w=worker(),done=vi.fn();let acknowledge!:()=>void;w.sendManualHistoryOnce.mockImplementation(()=>new Promise<void>(resolve=>{acknowledge=resolve;}));w.queryManualHistorySend.mockResolvedValueOnce(state('verifying')).mockResolvedValueOnce(state('sending')).mockResolvedValueOnce(state('sent'));
 render(<ManualHistorySend worker={w} job={job} onTerminal={done}/>);const send=await confirm();fireEvent.click(send);fireEvent.click(send);
 expect(w.sendManualHistoryOnce).toHaveBeenCalledTimes(1);expect(screen.queryByText(/^Message sent/)).not.toBeInTheDocument();expect(w.queryManualHistorySend).not.toHaveBeenCalled();
 await act(async()=>acknowledge());expect(screen.getByRole('status')).toHaveTextContent('Checking this chat');await screen.findByText('Sending one message…');
 expect(await screen.findByText('Message sent.')).toBeVisible();expect(done).toHaveBeenCalledTimes(1);expect(w.sendManualHistoryOnce).toHaveBeenCalledExactlyOnceWith({job_id:'job1',version:3,preview_receipt:'a'.repeat(64)});expect(screen.queryByRole('button',{name:'Send now'})).not.toBeInTheDocument();
});
it.each(['unknown','failed'] as const)('stops after a %s result and offers no blind resend',async(result)=>{
 const w=worker(),done=vi.fn();w.queryManualHistorySend.mockResolvedValue(state(result));render(<ManualHistorySend worker={w} job={job} onTerminal={done}/>);fireEvent.click(await confirm());
 await waitFor(()=>expect(done).toHaveBeenCalledTimes(1));expect(w.sendManualHistoryOnce).toHaveBeenCalledTimes(1);expect(w.queryManualHistorySend).toHaveBeenCalledTimes(1);expect(screen.queryByRole('button',{name:'Send now'})).not.toBeInTheDocument();if(result==='unknown')expect(screen.getByRole('status')).toHaveTextContent('Do not resend');
});
it('does not report success for another job or an unreadable result',async()=>{
 const w=worker();w.queryManualHistorySend.mockResolvedValue({...state('sent'),job_id:'another-job'});render(<ManualHistorySend worker={w} job={job}/>);fireEvent.click(await confirm());expect(await screen.findByText(/Outcome unknown\. Do not resend/)).toBeVisible();expect(screen.queryByText(/^Message sent/)).not.toBeInTheDocument();
});
it('reads status after an uncertain send acknowledgment without replaying the send',async()=>{
 const w=worker();w.sendManualHistoryOnce.mockRejectedValue(new Error('uncertain'));render(<ManualHistorySend worker={w} job={job}/>);fireEvent.click(await confirm());expect(await screen.findByText(/^Message sent/)).toBeVisible();expect(w.sendManualHistoryOnce).toHaveBeenCalledTimes(1);
});
it.each(['destination','sender','text'] as const)('rejects preview %s changes before exposing the final send action',async(change)=>{
 const w=worker(),value=preview();if(change==='destination')value.destination.chat_id='999';else if(change==='sender')value.target={...value.target,sender:{kind:'chat',id:'123'}};else value.text='Unexpected replacement';w.previewManualHistorySend.mockResolvedValue(value);
 render(<ManualHistorySend worker={w} job={job}/>);fireEvent.click(screen.getByRole('button',{name:'Send now'}));expect(await screen.findByText('Could not prepare this message. Refresh jobs and check Accounts.')).toBeVisible();expect(screen.queryByRole('dialog')).not.toBeInTheDocument();expect(w.sendManualHistoryOnce).not.toHaveBeenCalled();
});
it('discards a late preview when its job version changes',async()=>{
 const w=worker();let finish!:(value:ManualHistoryPreview)=>void;w.previewManualHistorySend.mockImplementation(()=>new Promise(resolve=>{finish=resolve;}));const ui=render(<ManualHistorySend worker={w} job={job}/>);fireEvent.click(screen.getByRole('button',{name:'Send now'}));ui.rerender(<ManualHistorySend worker={w} job={{...job,version:4}}/>);await act(async()=>finish(preview()));expect(screen.queryByRole('dialog')).not.toBeInTheDocument();expect(w.sendManualHistoryOnce).not.toHaveBeenCalled();
});
it('expires a preview instead of sending its old receipt',async()=>{
 const w=worker();w.previewManualHistorySend.mockImplementation(async()=>({...preview(),expires_at:new Date(Date.now()+100).toISOString()}));render(<ManualHistorySend worker={w} job={job}/>);await confirm();await waitFor(()=>expect(screen.queryByRole('dialog')).not.toBeInTheDocument());expect(screen.getByRole('status')).toHaveTextContent('Preview expired');expect(w.sendManualHistoryOnce).not.toHaveBeenCalled();
});
it('reports unknown when status cannot be read after dispatch',async()=>{
 const w=worker();w.queryManualHistorySend.mockRejectedValue(new Error('private transport detail'));render(<ManualHistorySend worker={w} job={job}/>);fireEvent.click(await confirm());expect(await screen.findByText(/Outcome unknown\. Do not resend/)).toBeVisible();expect(screen.queryByText(/private transport detail/)).not.toBeInTheDocument();expect(w.sendManualHistoryOnce).toHaveBeenCalledTimes(1);
});

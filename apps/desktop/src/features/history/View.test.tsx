import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { HistoryView } from './View';

it('distinguishes sending, final sent and unknown outcomes', async () => {
  const delivery={id:'d',post_id:'p',destination_id:'-456',content_hash:'a'.repeat(64),account_id:'123',account_epoch:1,sender:{kind:'user' as const,id:'123'},route_refs:[{route_id:'r',route_version:1,approval_id:null}] as [{route_id:string;route_version:number;approval_id:null}],attempt_id:null,message_id:null,temporary_message_id:null,final_message_id:null,request_extra:null};
  const worker={queryHistory:vi.fn().mockResolvedValue([{delivery:{...delivery,id:'a',state:'sending',temporary_message_id:'-7'},attempts:[]},{delivery:{...delivery,id:'b',state:'sent',message_id:'8',final_message_id:'8'},attempts:[]},{delivery:{...delivery,id:'c',state:'unknown'},attempts:[]}])};
  render(<HistoryView worker={worker}/>);
  expect(await screen.findByText('Sending')).toBeVisible();
  expect(screen.getByText('Sent')).toBeVisible();
  expect(screen.getByText(/May already have been delivered/)).toBeVisible();
  expect(screen.queryByRole('button',{name:/retry/i})).not.toBeInTheDocument();
});

it('pages archived outcomes and reads attempt details only on request',async()=>{
 const delivery={id:'delivery-1',post_id:'p',destination_id:'123',content_hash:'a'.repeat(64),account_id:'123',account_epoch:1,sender:{kind:'user',id:'123'},route_refs:[],attempt_id:null,message_id:null,temporary_message_id:null,final_message_id:null,request_extra:null,state:'unknown'};
 const worker={queryHistory:vi.fn().mockRejectedValue(new Error('unbounded legacy query forbidden')),queryArchiveDeliveries:vi.fn().mockResolvedValue({items:[{delivery,attempt_count:2,created_at:'2026-11-01T00:00:00Z',updated_at:'2026-11-01T00:01:00Z'}],next_cursor:'20'}),queryArchiveDelivery:vi.fn().mockResolvedValue({delivery,attempts:[{delivery_id:'delivery-1',attempt_id:'attempt-1',request_extra:'request-1',temporary_message_id:'-7',final_message_id:null,state:'unknown'}]})};
 render(<HistoryView worker={worker}/>);
 expect(await screen.findByText(/2 attempts/)).toBeVisible();expect(worker.queryArchiveDelivery).not.toHaveBeenCalled();expect(worker.queryHistory).not.toHaveBeenCalled();
 fireEvent.click(screen.getByRole('button',{name:'Next deliveries page'}));await waitFor(()=>expect(worker.queryArchiveDeliveries).toHaveBeenLastCalledWith({cursor:'20',limit:20,search:''}));
 fireEvent.change(screen.getByLabelText('Search delivery outcomes'),{target:{value:'123'}});fireEvent.click(screen.getByRole('button',{name:'Search deliveries'}));await waitFor(()=>expect(worker.queryArchiveDeliveries).toHaveBeenLastCalledWith({cursor:null,limit:20,search:'123'}));
 fireEvent.click(screen.getByRole('button',{name:'Inspect delivery delivery-1'}));expect(await screen.findByText('request-1')).toBeVisible();expect(worker.queryArchiveDelivery).toHaveBeenCalledWith('delivery-1');
});

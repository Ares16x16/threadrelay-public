import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { RouteQueue, queueReason } from './RouteQueue';
it('explains provider waiting, rejection codes and uncertain outcomes',()=>{
 expect(queueReason('telegram_flood_wait:15','waiting')).toBe('Telegram requires a wait of 15 seconds. The queue will wait before the next send.');
 expect(queueReason('telegram_rejected:403','failed')).toContain('error 403');
 expect(queueReason('outcome_unknown;telegram_flood_wait:20','unknown')).toContain('unconfirmed and will not be retried automatically');
 expect(queueReason('telegram_slowmode_wait:9','waiting')).toContain('for this chat');
 expect(queueReason('classification_not_current','blocked')).toContain('Collect again with the current route');
});

it('clears the whole selected route once after confirmation and leaves its queue paused',async()=>{
 let finish!:()=>void;
 const worker={queryRouteQueue:vi.fn().mockResolvedValue({items:[],paused:false,next_cursor:'next-page'}),clearRouteQueue:vi.fn(()=>new Promise<void>(resolve=>{finish=resolve;})),cancelRouteQueueItem:vi.fn(),setRouteQueuePaused:vi.fn()};
 render(<RouteQueue worker={worker} routeId="test" onClose={vi.fn()}/>);
 fireEvent.click(await screen.findByRole('button',{name:'Next page'}));
 await waitFor(()=>expect(worker.queryRouteQueue).toHaveBeenLastCalledWith({route_id:'test',cursor:'next-page'}));
 const clear=await screen.findByRole('button',{name:'Clear queue'});
 await waitFor(()=>expect(clear).toBeEnabled());
 fireEvent.click(clear);
 const dialog=screen.getByRole('dialog',{name:'Clear this queue?'});
 expect(dialog).toHaveTextContent('test');
 expect(dialog).toHaveTextContent(/every page/);
 expect(dialog).toHaveTextContent(/Permanent History is kept/);
 expect(worker.clearRouteQueue).not.toHaveBeenCalled();
 const confirm=within(dialog).getByRole('button',{name:'Clear queue'});
 fireEvent.click(confirm);fireEvent.click(confirm);
 expect(worker.clearRouteQueue).toHaveBeenCalledExactlyOnceWith('test');
 worker.queryRouteQueue.mockResolvedValue({items:[],paused:true,next_cursor:null});
 await act(async()=>finish());
 expect(await screen.findByText('Queue cleared and paused.')).toBeVisible();
 expect(await screen.findByRole('button',{name:'Resume queue'})).toBeEnabled();
 expect(worker.queryRouteQueue).toHaveBeenLastCalledWith({route_id:'test'});
 expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
 expect(worker.cancelRouteQueueItem).not.toHaveBeenCalled();
 expect(worker.setRouteQueuePaused).not.toHaveBeenCalled();
});

it('cancels queue clearing without sending a mutation',async()=>{
 const worker={queryRouteQueue:vi.fn().mockResolvedValue({items:[],paused:false,next_cursor:null}),clearRouteQueue:vi.fn()};
 render(<RouteQueue worker={worker} routeId="news" onClose={vi.fn()}/>);
 const clear=await screen.findByRole('button',{name:'Clear queue'});
 await waitFor(()=>expect(clear).toBeEnabled());fireEvent.click(clear);
 fireEvent.click(within(screen.getByRole('dialog')).getByRole('button',{name:'Cancel'}));
 expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
 expect(worker.clearRouteQueue).not.toHaveBeenCalled();
});

it('keeps clearing unavailable when the worker has no clear command',async()=>{
 render(<RouteQueue worker={{queryRouteQueue:vi.fn().mockResolvedValue({items:[],paused:false,next_cursor:null})}} routeId="news" onClose={vi.fn()}/>);
 await waitFor(()=>expect(screen.getByRole('button',{name:'Clear queue'})).toBeDisabled());
});

it('keeps a rejected clear visible in its dialog without claiming success or retrying',async()=>{
 const worker={queryRouteQueue:vi.fn().mockResolvedValue({items:[],paused:false,next_cursor:null}),clearRouteQueue:vi.fn().mockRejectedValue(new Error('unavailable'))};
 render(<RouteQueue worker={worker} routeId="news" onClose={vi.fn()}/>);
 const clear=await screen.findByRole('button',{name:'Clear queue'});
 await waitFor(()=>expect(clear).toBeEnabled());fireEvent.click(clear);
 const dialog=screen.getByRole('dialog');
 fireEvent.click(within(dialog).getByRole('button',{name:'Clear queue'}));
 expect(await within(dialog).findByRole('alert')).toHaveTextContent('Queue change was not confirmed');
 expect(screen.queryByText('Queue cleared and paused.')).not.toBeInTheDocument();
 expect(worker.clearRouteQueue).toHaveBeenCalledExactlyOnceWith('news');
});


it('offers one explicit retry of pristine blocked metadata checks without invoking recovery',async()=>{
 const worker={queryRouteQueue:vi.fn().mockResolvedValue({items:[{id:'waiting',route_id:'test',route_version:1,text:'Saved link',destination_id:'-20',destination_title:'Chosen group',due_at:null,state:'blocked',reason:'verification_failed'}],paused:false,next_cursor:null}),setRouteQueuePaused:vi.fn().mockResolvedValue(undefined),beginTelegramRecovery:vi.fn()};
 render(<RouteQueue worker={worker} routeId="test" onClose={vi.fn()}/>);
 const retry=await screen.findByRole('button',{name:'Retry checks'});
 fireEvent.click(retry);fireEvent.click(retry);
 await waitFor(()=>expect(worker.setRouteQueuePaused).toHaveBeenCalledExactlyOnceWith('test',false));
 expect(worker.beginTelegramRecovery).not.toHaveBeenCalled();
 expect(screen.getByText(/No send attempt was made/)).toBeVisible();
});

import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ActivityMonitor, ActivityView } from './View';
import { entry, page } from './testFixtures';
afterEach(()=>vi.useRealTimers());
it('shows newest events first with previews, Hong Kong times and distinct delivery outcomes',async()=>{
 const worker={queryActivity:vi.fn().mockResolvedValue(page([
  {...entry('1'),kind:'history_saved',revision_id:'revision-1'},
  {...entry('3'),category:'model',kind:'classification_succeeded',result:'match'},
  {...entry('4'),category:'delivery',kind:'delivery_pending',destination:'Saved Messages',due_at:'2026-11-01T04:05:00Z'},
  {...entry('5'),category:'delivery',kind:'delivery_preparing'},
  {...entry('6'),category:'delivery',kind:'delivery_sending'},
  {...entry('7'),category:'delivery',kind:'delivery_sent'},
  {...entry('8'),category:'delivery',kind:'delivery_unknown'},
  {...entry('9'),kind:'reading',detail:'visible_feed'},
  {...entry('10'),kind:'skipped',detail:'unknown_layout'},
  {...entry('11'),category:'session',kind:'session_attention',detail:'unknown_layout'},
 ]))};
 const history=vi.fn();render(<ActivityView worker={worker} onOpenHistory={history}/>);
 expect(await screen.findByText('Sent — confirmed by Telegram')).toBeVisible();
 expect(screen.getByText('Awaiting Telegram confirmation')).toBeVisible();expect(screen.getByText('Preparing delivery')).toBeVisible();
 expect(screen.getByText(/Check Telegram before any retry/)).toBeVisible();expect(screen.getByText('Match')).toBeVisible();
 expect(screen.getByText('Reading visible feed')).toBeVisible();
 expect(screen.getByText('Unsupported card skipped.')).toBeVisible();
 expect(screen.getByText(/Move to another post in Threads, then choose Start collecting/)).toBeVisible();
 expect(screen.getByText('01 Nov, 12:05:00')).toBeVisible();
 const events=screen.getByRole('list');expect(within(events).getAllByRole('listitem').map(item=>item.dataset.sequence)).toEqual(['11','10','9','8','7','6','5','4','3','1']);
 fireEvent.change(screen.getByLabelText('Show activity'),{target:{value:'model'}});expect(within(events).getAllByRole('listitem')).toHaveLength(1);
 fireEvent.click(screen.getByRole('button',{name:'Open History'}));expect(history).toHaveBeenCalledOnce();
});
it('labels frozen and failed reads honestly and retries only the read',async()=>{
 vi.useFakeTimers();const worker={queryActivity:vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue(page([entry('1')]))};
 render(<ActivityMonitor worker={worker} onOpenHistory={()=>{}}/>);await act(async()=>{});
 expect(screen.getByText('Updates unavailable')).toBeVisible();expect(screen.queryByText('Following live')).not.toBeInTheDocument();
 fireEvent.click(screen.getByRole('button',{name:'Retry activity read'}));await act(async()=>{});expect(screen.getByText('Following live')).toBeVisible();
 fireEvent.click(screen.getByRole('button',{name:'Freeze display'}));expect(screen.getByText('Display frozen')).toBeVisible();
 const reads=worker.queryActivity.mock.calls.length;await act(async()=>vi.advanceTimersByTimeAsync(5000));expect(worker.queryActivity).toHaveBeenCalledTimes(reads);
 expect(screen.getByText(/These controls do not start, stop or reconnect/)).toBeVisible();
});
it('warns when recording is incomplete and keeps pagination out of the compact monitor',async()=>{
 const worker={queryActivity:vi.fn().mockResolvedValue(page([entry('2')],{older_cursor:'2',recording_ok:false}))};
 render(<ActivityMonitor compact worker={worker} onOpenHistory={()=>{}}/>);
 expect(await screen.findByText('Updates incomplete')).toBeVisible();expect(screen.getByRole('alert')).toHaveTextContent('timeline is incomplete');
 expect(screen.queryByRole('button',{name:'Load older events'})).not.toBeInTheDocument();expect(screen.getByText('Visible post 2')).toBeVisible();
});

it('shows only current running recovery and translates bounded recovery details',async()=>{
 const worker={queryActivity:vi.fn().mockResolvedValue(page([{...entry('1'),kind:'collection_recovering',detail:'unsupported_cards:2:4'},{...entry('2'),kind:'collection_recovered',detail:'validated_post'}],{session:{state:'running',counters:{observed:0,matched:0,queued:0,sent:0},attention_reason:null},recovery:{reason:'unknown_layout',attempt:2,retry_in_seconds:4}}))};
 render(<ActivityMonitor worker={worker} onOpenHistory={()=>{}}/>);
 expect(await screen.findByText(/Recovering feed.*next check in 4s/)).toBeVisible();
 expect(screen.getByText(/Unsupported cards were skipped.*4s/)).toBeVisible();
 expect(screen.getByText(/A supported post is available/)).toBeVisible();
 fireEvent.click(screen.getByRole('button',{name:'Freeze display'}));expect(screen.queryByText(/Recovering feed.*next check in/)).not.toBeInTheDocument();
});

it('never promotes a historic recovery event after collection stopped or prints unrecognized recovery codes',async()=>{
 const worker={queryActivity:vi.fn().mockResolvedValue(page([{...entry('1'),kind:'collection_recovering',detail:'unknown_layout:bad:999'}],{recovery:{reason:'unknown_layout',attempt:1,retry_in_seconds:4}}))};
 render(<ActivityMonitor worker={worker} onOpenHistory={()=>{}}/>);
 expect(await screen.findByText('Feed recovery is being checked.')).toBeVisible();
 expect(screen.queryByText(/next check in 4s/)).not.toBeInTheDocument();expect(screen.queryByText('unknown_layout:bad:999')).not.toBeInTheDocument();
});

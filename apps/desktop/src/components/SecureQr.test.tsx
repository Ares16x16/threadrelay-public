import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { SecureQr } from './SecureQr';
import { StrictMode } from 'react';

describe('ephemeral QR surface', () => {
  it('automatically shows a deliberately requested QR once under StrictMode without exposing its token',async()=>{
    vi.useFakeTimers();
    try {
      vi.spyOn(HTMLCanvasElement.prototype,'getContext').mockReturnValue({drawImage:vi.fn()} as unknown as CanvasRenderingContext2D);
      const take=vi.fn().mockResolvedValue('synthetic-private-auto-qr');
      const encode=vi.fn(async(canvas:HTMLCanvasElement)=>{canvas.width=24;canvas.height=24;});
      const {container}=render(<StrictMode><SecureQr challengeId="auto-qr" epoch={4} expiresAt={null} take={take} encode={encode} autoShow/></StrictMode>);
      expect(take).not.toHaveBeenCalled();
      await act(async()=>{await vi.runOnlyPendingTimersAsync();});
      expect(take).toHaveBeenCalledTimes(1);expect(encode).toHaveBeenCalledTimes(1);
      expect(screen.getByRole('img',{name:'Telegram login QR'})).toBeVisible();
      expect(container.innerHTML).not.toContain('synthetic-private-auto-qr');
    } finally {vi.useRealTimers();}
  });
  it('consumes only after deliberate click and never renders the token as text or an attribute', async () => {
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({drawImage:vi.fn()} as unknown as CanvasRenderingContext2D);
    const take = vi.fn().mockResolvedValue('synthetic-private-qr');
    const encode = vi.fn(async (_canvas:HTMLCanvasElement, _value:string) => {});
    const { container, unmount } = render(<SecureQr challengeId="c1" epoch={3} expiresAt={null} take={take} encode={encode}/>);
    expect(take).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', {name:'Show login QR'}));
    await waitFor(() => expect(encode).toHaveBeenCalledTimes(1));
    expect(take).toHaveBeenCalledWith('c1',3);
    expect(container.innerHTML).not.toContain('synthetic-private-qr');
    unmount();
  });
  it('does not encode a late result after the component is removed', async () => {
    let finish!:(value:string)=>void;
    const take = vi.fn(()=>new Promise<string>(resolve=>{finish=resolve;}));
    const encode = vi.fn(async()=>{});
    const { unmount } = render(<SecureQr challengeId="old" epoch={4} expiresAt={null} take={take} encode={encode}/>);
    fireEvent.click(screen.getByRole('button', {name:'Show login QR'}));
    unmount(); finish('synthetic-private-qr');
    await Promise.resolve(); await Promise.resolve();
    expect(encode).not.toHaveBeenCalled();
  });
  it('erases the canvas at expiry without echoing the token', async () => {
    vi.useFakeTimers();
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({drawImage:vi.fn()} as unknown as CanvasRenderingContext2D);
    const take=vi.fn().mockResolvedValue('synthetic-ephemeral-qr');
    const encode=vi.fn(async (canvas:HTMLCanvasElement)=>{canvas.width=24;canvas.height=24;});
    const expiresAt=new Date(Date.now()+1000).toISOString();
    const {container,unmount}=render(<SecureQr challengeId="c" epoch={1} expiresAt={expiresAt} take={take} encode={encode}/>);
    fireEvent.click(screen.getByRole('button',{name:'Show login QR'}));
    await act(async()=>{await Promise.resolve();await Promise.resolve();});
    const canvas=container.querySelector('canvas')!; expect(canvas.width).toBe(24);
    act(()=>vi.advanceTimersByTime(1000));
    expect(canvas.width).toBe(0); expect(canvas.height).toBe(0);
    expect(container.innerHTML).not.toContain('synthetic-ephemeral-qr');
    unmount();vi.useRealTimers();
  });

  it.each([-1, 1, null])('preserves shown QR pixels across expiry projection drift %s and keeps the earliest deadline', async drift => {
    vi.useFakeTimers();
    try {
      vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({drawImage:vi.fn()} as unknown as CanvasRenderingContext2D);
      const take=vi.fn().mockResolvedValue('synthetic-private-qr');
      const encode=vi.fn(async (canvas:HTMLCanvasElement)=>{canvas.width=24;canvas.height=24;});
      const deadline=Date.now()+20000;
      const props={challengeId:'same',epoch:4,take,encode};
      const {container,rerender}=render(<SecureQr {...props} expiresAt={new Date(deadline).toISOString()}/>);
      fireEvent.click(screen.getByRole('button',{name:'Show login QR'}));
      await act(async()=>{await Promise.resolve();await Promise.resolve();});
      const canvas=container.querySelector('canvas')!;
      expect(canvas.width).toBe(24);
      rerender(<SecureQr {...props} expiresAt={drift===null?null:new Date(deadline+drift).toISOString()}/>);
      expect(canvas.width).toBe(24);
      expect(canvas.height).toBe(24);
      expect(take).toHaveBeenCalledTimes(1);
      act(()=>vi.advanceTimersByTime(20000+Math.min(drift??0,0)));
      expect(canvas.width).toBe(0);
      expect(canvas.height).toBe(0);
      expect(screen.getByRole('button',{name:'Show login QR'})).toBeDisabled();
      expect(container.innerHTML).not.toContain('synthetic-private-qr');
    } finally {vi.useRealTimers();}
  });

});

import { act,fireEvent,render,screen } from '@testing-library/react';
import { expect,it,vi } from 'vitest';
import { LoginCodeDelivery } from './LoginCodeDelivery';

it('shows the provider channel and counts down without automatically requesting another code',async()=>{
 vi.useFakeTimers();
 try {
  const resend=vi.fn();
  render(<LoginCodeDelivery delivery={{method:'telegram',next_method:'sms',resend_after_seconds:2}} pending={false} resend={resend}/>);
  expect(screen.getByText(/verified Telegram service chat/)).toBeVisible();
  expect(screen.getByRole('button',{name:'Resend available in 2s'})).toBeDisabled();
  await act(async()=>{await vi.advanceTimersByTimeAsync(2000);});
  expect(resend).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button',{name:'Resend code via SMS'}));
  expect(resend).toHaveBeenCalledOnce();
 } finally {vi.useRealTimers();}
});

it('does not invent a resend option when Telegram offered no next method',()=>{
 render(<LoginCodeDelivery delivery={{method:'sms',next_method:null,resend_after_seconds:null}} pending={false} resend={vi.fn()}/>);
 expect(screen.getByText(/sent this code by SMS/)).toBeVisible();
 expect(screen.queryByRole('button')).not.toBeInTheDocument();
});

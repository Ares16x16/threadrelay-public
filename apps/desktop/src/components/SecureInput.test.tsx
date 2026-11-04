import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { SecureInput } from './SecureInput';

it('clears a masked input before awaiting a failing secure handoff', async () => {
  let resolve!: () => void;
  const send = vi.fn(() => new Promise<void>((r) => { resolve = r; }));
  render(<SecureInput label="Password" kind="password" onSubmit={send} onCancel={() => {}} />);
  const field = screen.getByLabelText('Password');
  expect(field).toHaveAttribute('type', 'password');
  fireEvent.change(field, {target:{value:'test-only'}});
  fireEvent.click(screen.getByRole('button', {name:'Continue'}));
  expect(field).toHaveValue('');
  expect(send).toHaveBeenCalledWith('test-only');
  resolve();
});

it('clears on cancel', () => {
  render(<SecureInput label="Code" kind="code" onSubmit={async () => {}} onCancel={() => {}} />);
  const field = screen.getByLabelText('Code');
  fireEvent.change(field, {target:{value:'0012'}});
  fireEvent.click(screen.getByRole('button', {name:'Cancel'}));
  expect(field).toHaveValue('');
});
it('does not dispatch a second submission while one is pending', () => {
  const send=vi.fn(()=>new Promise<void>(()=>{}));
  render(<SecureInput label="Code" kind="code" onSubmit={send} onCancel={()=>{}} />);
  const field=screen.getByLabelText('Code');
  fireEvent.change(field,{target:{value:'0012'}});
  const form=field.closest('form')!;
  fireEvent.submit(form);
  fireEvent.change(field,{target:{value:'3456'}});
  fireEvent.submit(form);
  expect(send).toHaveBeenCalledTimes(1);
  expect(field).toHaveValue('');
});

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { SecureCredentials } from './SecureCredentials';

const syntheticHash = '0123456789abcdef0123456789ABCDEF';

it.each([
  ['0', syntheticHash],
  ['2147483648', syntheticHash],
  ['123', 'not-a-telegram-api-hash'],
  ['123', 'g'.repeat(32)],
  ['123', 'a'.repeat(33)],
])('rejects invalid developer credentials before native storage: ID %s', async (apiId, apiHash) => {
  const submit = vi.fn();
  render(<SecureCredentials onSubmit={submit} onCancel={() => {}} />);
  const id = screen.getByLabelText('Developer API ID');
  const hash = screen.getByLabelText('Developer API hash');
  fireEvent.change(id, { target: { value: apiId } });
  fireEvent.change(hash, { target: { value: apiHash } });
  fireEvent.submit(id.closest('form')!);
  expect(submit).not.toHaveBeenCalled();
  expect(await screen.findByRole('alert')).toHaveTextContent(/valid API ID.*32/);
  expect(id).toHaveValue('');
  expect(hash).toHaveValue('');
});

it.each(['1', '2147483647'])('clears valid credentials before the private handoff at ID %s', async apiId => {
  const submit = vi.fn().mockResolvedValue(undefined);
  render(<SecureCredentials onSubmit={submit} onCancel={() => {}} />);
  const id = screen.getByLabelText('Developer API ID');
  const hash = screen.getByLabelText('Developer API hash');
  fireEvent.change(id, { target: { value: apiId } });
  fireEvent.change(hash, { target: { value: syntheticHash } });
  fireEvent.submit(id.closest('form')!);
  expect(id).toHaveValue('');
  expect(hash).toHaveValue('');
  await waitFor(() => expect(submit).toHaveBeenCalledWith(apiId, syntheticHash));
});

import { describe, expect, it } from 'vitest';
import fixtures from '../../../../contracts/fixtures.json';
import { validateContract } from './contracts';

describe('shared runtime contracts', () => {
  for (const fixture of fixtures.valid) {
    it(`accepts ${fixture.name}`, () => {
      expect(validateContract(fixture.contract, fixture.value)).toBe(true);
    });
  }
  for (const fixture of fixtures.invalid) {
    it(`rejects ${fixture.name}`, () => {
      expect(validateContract(fixture.contract, fixture.value)).toBe(false);
    });
  }
  it('fails closed for unrecognized contract names', () => {
    expect(validateContract('NotAContract', {})).toBe(false);
  });
});

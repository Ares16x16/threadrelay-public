import {expect,it,vi} from 'vitest';
import {SecureAuthClient} from './secureAuth';
it('accepts only known static sign-in blocker reasons while preserving older status replies',async()=>{
 const invoke=vi.fn().mockResolvedValueOnce({available:false,configured:true,reason:'vault_unavailable'}).mockResolvedValueOnce({available:false,configured:false,reason:'untrusted private detail'}).mockResolvedValueOnce({available:true,configured:true,reason:'vault_unavailable'});
 const client=new SecureAuthClient(invoke);
 expect(await client.status()).toEqual({available:false,configured:true,reason:'vault_unavailable'});
 await expect(client.status()).rejects.toThrow('Secure status unavailable.');
 await expect(client.status()).rejects.toThrow('Secure status unavailable.');
});
it('does not query or activate anything on construction and reads only fixed host status',async()=>{
 const invoke=vi.fn().mockResolvedValue({available:true,configured:true});const client=new SecureAuthClient(invoke);
 expect(client.available).toBe(false);expect(invoke).not.toHaveBeenCalled();
 expect(await client.status()).toEqual({available:true,configured:true});expect(invoke).toHaveBeenCalledWith('secure_status',{});expect(client.available).toBe(true);
});
it('closes a previously available presentation after malformed or unavailable status',async()=>{
 const invoke=vi.fn().mockResolvedValueOnce({available:true,configured:true}).mockResolvedValueOnce({available:true,configured:true,untrusted:'extra'}).mockRejectedValueOnce(new Error('fixture'));
 const client=new SecureAuthClient(invoke);await client.status();await expect(client.status()).rejects.toThrow('Secure status unavailable.');expect(client.available).toBe(false);await expect(client.status()).rejects.toThrow();expect(client.available).toBe(false);
});

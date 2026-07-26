import assert from 'node:assert/strict'
import fs from 'node:fs'

import { test, vi } from 'vitest'

function loadGatewayAuthProviders(fetchPublicJson: (url: string, options: { timeoutMs: number }) => Promise<unknown>) {
  const mainSource = fs.readFileSync(new URL('./main.ts', import.meta.url), 'utf8')
  const start = mainSource.indexOf('const gatewayAuthProvidersCache')
  const end = mainSource.indexOf('// Build the readiness probe', start)

  assert.notEqual(start, -1, 'gatewayAuthProviders cache declaration must exist in main.ts')
  assert.notEqual(end, -1, 'gatewayAuthProviders source boundary must exist in main.ts')

  const executableSource = mainSource
    .slice(start, end)
    .replace('new Map<string, any[]>()', 'new Map()')
    .replace(') as any', ')')

  return new Function('fetchPublicJson', `${executableSource}\nreturn gatewayAuthProviders`)(fetchPublicJson) as (
    baseUrl: string
  ) => Promise<Array<{ name: string; supportsPassword: boolean }>>
}

test('gatewayAuthProviders retries after a transient fetch failure', async () => {
  const fetchPublicJson = vi
    .fn()
    .mockRejectedValueOnce(new Error('temporary network failure'))
    .mockResolvedValueOnce({ providers: [{ name: 'password', supports_password: true }] })

  const gatewayAuthProviders = loadGatewayAuthProviders(fetchPublicJson)

  assert.deepEqual(await gatewayAuthProviders('https://gateway.example.com'), [])
  assert.deepEqual(await gatewayAuthProviders('https://gateway.example.com'), [
    { name: 'password', supportsPassword: true }
  ])
  assert.equal(fetchPublicJson.mock.calls.length, 2)
})

test('gatewayAuthProviders caches a successful empty providers list', async () => {
  const fetchPublicJson = vi.fn().mockResolvedValue({ providers: [] })
  const gatewayAuthProviders = loadGatewayAuthProviders(fetchPublicJson)

  assert.deepEqual(await gatewayAuthProviders('https://gateway.example.com'), [])
  assert.deepEqual(await gatewayAuthProviders('https://gateway.example.com'), [])
  assert.equal(fetchPublicJson.mock.calls.length, 1)
})

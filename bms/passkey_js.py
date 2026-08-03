"""Browser-side WebAuthn, shared by the tenant page and the operator UI.

WebAuthn moves binary fields as ArrayBuffers, and JSON cannot carry those. Every
implementation therefore needs the same base64url conversions in both directions,
and getting one of them wrong produces an opaque `DOMException` with no
indication of which field was malformed. Written once here rather than twice.

The challenge is echoed back to the server as `_challenge` alongside the
credential. The server issued it, stored it, and deletes it on use -- sending it
back is how the server knows *which* pending challenge this response answers,
not a trust decision. A forged value simply matches no stored row.
"""

PASSKEY_JS = """
function b64urlToBytes(value) {
  const pad = '='.repeat((4 - value.length % 4) % 4);
  const raw = atob((value + pad).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}
function bytesToB64url(buffer) {
  const bytes = new Uint8Array(buffer);
  let s = '';
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, '');
}

function passkeySupported() {
  return typeof PublicKeyCredential !== 'undefined' &&
         typeof navigator.credentials !== 'undefined' &&
         window.isSecureContext;
}

async function passkeyRegister(label) {
  const options = await api('POST', '/passkeys/register/options');
  const challenge = options.challenge;

  options.challenge = b64urlToBytes(options.challenge);
  options.user.id = b64urlToBytes(options.user.id);
  (options.excludeCredentials || []).forEach(c => { c.id = b64urlToBytes(c.id); });

  const cred = await navigator.credentials.create({ publicKey: options });
  return api('POST', '/passkeys/register', {
    label: label || 'phone',
    credential: {
      id: cred.id,
      rawId: bytesToB64url(cred.rawId),
      type: cred.type,
      _challenge: challenge,
      response: {
        clientDataJSON: bytesToB64url(cred.response.clientDataJSON),
        attestationObject: bytesToB64url(cred.response.attestationObject)
      }
    }
  });
}

// Prompts for Face ID / fingerprint and returns once the server has accepted the
// assertion. The verification is then good for a short window, so unlocking two
// doors in a row does not mean two face scans.
async function passkeyVerify() {
  const options = await api('POST', '/passkeys/verify/options');
  const challenge = options.challenge;

  options.challenge = b64urlToBytes(options.challenge);
  (options.allowCredentials || []).forEach(c => { c.id = b64urlToBytes(c.id); });

  const cred = await navigator.credentials.get({ publicKey: options });
  return api('POST', '/passkeys/verify', {
    credential: {
      id: cred.id,
      rawId: bytesToB64url(cred.rawId),
      type: cred.type,
      _challenge: challenge,
      response: {
        clientDataJSON: bytesToB64url(cred.response.clientDataJSON),
        authenticatorData: bytesToB64url(cred.response.authenticatorData),
        signature: bytesToB64url(cred.response.signature),
        userHandle: cred.response.userHandle ? bytesToB64url(cred.response.userHandle) : null
      }
    }
  });
}

// Run an action that needs a verified person present. Verifies first if the
// server says so, then retries once -- so the common path is a single tap and
// the step-up only appears when it is actually needed.
async function withVerification(run) {
  try {
    return await run();
  } catch (err) {
    if (!/verify_required|verification required/i.test(err.message || '')) throw err;
    await passkeyVerify();
    return run();
  }
}
"""

# Official x402 adapters

The 0.2 stack composes the upstream x402 2.21.0 runtime instead of duplicating
transport models or codecs.

- Client: `x402Client` and `x402AsyncTransport`
- Resource server: `x402ResourceServer` and HTTP middleware
- Facilitator: `x402FacilitatorSync`
- MCP: `create_payment_wrapper`, `wrap_mcp_client_with_payment`, and
  `create_x402_mcp_client`
- Extensions: upstream payment-identifier and Bazaar helpers

Register `ExactXRPLClientScheme`, `ExactXRPLServerScheme`, or
`ExactXRPLFacilitatorScheme` for the appropriate role. Default registration
covers `xrpl:0`, `xrpl:1`, and `xrpl:2`.

The XRPL scheme payload is `{ "signedTxBlob": "..." }`. Asset data belongs in
official requirements: XRP uses `asset: "XRP"`; IOUs use the currency code and
`extra.issuer`.

The stack intentionally does not claim XRPL support for EVM-specific gas
sponsorship, ERC builder codes, or SIWX. Unknown extension data remains opaque
and is preserved.

# Authorization payment flow

1. The client requests a protected resource.
2. The server returns HTTP 402 with `PAYMENT-REQUIRED`.
3. The client selects an allowed requirement, signs one exact XRPL Payment, and
   retries once with `PAYMENT-SIGNATURE` if the body is replayable.
4. The resource server asks the facilitator to verify the payload.
5. If valid, the protected handler runs. Its complete response is buffered.
6. Only a successful 2xx/3xx handler response is submitted for settlement.
7. The facilitator reserves the transaction hash, submits once, and waits for a
   validated ledger result.
8. The server releases the buffered response with `PAYMENT-RESPONSE` only after
   settlement succeeds.

Invalid payments never run the handler. Handler errors and 4xx/5xx responses
are never settled. Pending settlement retries reuse the same envelope; an
unsafe handler is recovered from Redis rather than executed twice.

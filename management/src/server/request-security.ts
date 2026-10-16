/**
 * Device-management mutations require a non-simple JSON request. Besides
 * making the wire contract explicit, this prevents a cross-origin HTML form
 * from invoking them without a CORS preflight. The deployment's Ingress or
 * trusted direct-access boundary remains the authentication authority.
 */
export function requireJsonMutation(request: Request): Response | undefined {
  if (
    request.headers.get('content-type')?.split(';', 1)[0]?.trim() ===
    'application/json'
  ) {
    return undefined;
  }
  return Response.json(
    {
      error: 'unsupported_media_type',
      message: 'Management mutations require application/json.',
    },
    {
      status: 415,
      headers: { 'Cache-Control': 'no-store' },
    },
  );
}

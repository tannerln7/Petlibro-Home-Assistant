import {
  parseSetupPreflightResult,
  type SetupPreflightRequest,
} from '#/domain/setup';

export async function runSetupPreflight(input: SetupPreflightRequest) {
  const response = await fetch(
    `${import.meta.env.BASE_URL}api/v1/setup/preflight`,
    {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(input),
    },
  );
  const value = (await response.json()) as unknown;
  if (!response.ok) {
    const message =
      typeof value === 'object' &&
      value !== null &&
      'message' in value &&
      typeof value.message === 'string'
        ? value.message
        : 'Setup preflight failed.';
    throw new Error(message);
  }
  return parseSetupPreflightResult(value);
}

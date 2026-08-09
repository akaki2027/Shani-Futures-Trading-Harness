import { Portal } from '@/components/Portal';

// Thin by design. Everything below is a client of the Shani API and holds no
// business logic of its own — see the note at the top of lib/api.ts.
export default function Page() {
  return <Portal />;
}

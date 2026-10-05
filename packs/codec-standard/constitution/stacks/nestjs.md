# Layer 3 · Stack: NestJS

- Keep controllers thin; business rules live in services and domain classes with no framework imports.
- Validate every request body with DTOs and class-validator; never trust client input.
- Unit tests with Jest next to the code (`*.spec.ts`); e2e tests in `test/` with Supertest.
- Money is an integer number of cents, never a float.
- Database changes go through migrations; never edit an applied migration.

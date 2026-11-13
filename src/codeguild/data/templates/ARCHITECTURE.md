# Architecture

## Stack
- Language:
- Framework:
- Database:
- Auth:
- Tests:
- Deployment:

## Data flow
user → UI → service/API → storage

## Folder structure
```
src/
tests/
```

## Rules
- UI does not contain database logic.
- Business logic stays out of UI components.
- Validate all input at the boundary.

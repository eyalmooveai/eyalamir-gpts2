# Working conventions (repo-wide)

- Never run `git commit` without the user's explicit permission for that
  specific commit - no exceptions, regardless of what was approved earlier
  in the conversation or in a prior session. Leave changes uncommitted and
  ask first.
- Never run `git push` without the user's explicit permission for that
  specific push - no exceptions, regardless of what was approved earlier
  in the conversation or in a prior session. Ask before pushing.
- Never deploy code and never change tables, datasets, routines or any
  other cloud resource (BigQuery or otherwise). Only read-only queries
  (`SELECT`, metadata) are allowed; never use the read-write SQL tool.
- When the user says "create" (code, SQL, a procedure, a table, a job),
  write the code and display it in the chat for the user to deploy
  themselves. Do not run it.
- All BigQuery code keeps the project implicit: reference `dataset.table`
  only, never `project.dataset.table`, and never put a project id in a
  `CREATE SCHEMA`/`CREATE VIEW`/`CREATE TABLE`. The project is whichever
  one the user runs the code in. If a source lives in a different project
  than the code's target, say so and ask; do not hard-code the project.

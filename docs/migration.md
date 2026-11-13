# Migration and rollback

A serial adapter must have one owner. Installing a new broker while an existing
tool owns the adapter can split output, lose bytes, or interfere with a command.

## Handoff from an existing setup

1. Back up the current serial configuration and the scripts that use it.
2. Record the adapter's stable device path, baud rate, permissions, and owning
   service. Record current board state without sending a command.
3. Ask the active operator or agent to finish its current serial operation,
   close the port, and acknowledge that it will not reopen it during migration.
   An empty `fuser` snapshot alone is not an acknowledgement: a script may open
   the UART again a moment later.
4. Validate and render the Serial Party configuration before activation.
5. Stop or disable the previous serial owner only as part of the agreed handoff.
   The Serial Party installer does not kill unknown holders to make room.
6. Activate Serial Party using the documented host procedure. Preserve the
   resulting backup directory.
7. Test a read-only observer, then a writer acquisition/release without target
   input. Confirm that a competing writer is refused.
8. Migrate compatible tools to the client API, then explicitly let the operator
   or agent resume. Observe ordinary target output to verify the full path.

Do not reset a board, change boot media, or cycle its power for this handoff.

## Adding or changing boards

Prepare the updated host configuration and matching Home Assistant board list
together. Board IDs and port assignments must match. Treat broker restarts as a
maintenance window for every configured board; pause their writers first.

Keep the previous configuration and installation backup. Follow the current
host install/rollback help for the supported lifecycle rather than overwriting
generated files in a running service by hand.

## Rolling back

Coordinate another serial pause. Close web controllers and stop agent attempts
before running the host rollback command with its recorded backup directory.
Rollback restores managed host files and original device access settings; it
does not rewind commands already executed on a board.

Restore migrated agent scripts separately if returning to raw-port tooling.
Do not leave new and old tools racing for the same device. Dashboard and Home
Assistant app configuration are also separate from the host backup: restore only
the card/app changes being reversed, using the supported Home Assistant APIs.

## Updating from the original single-board prototype

The original local deployment used board-specific names and paths. This public
project deliberately does not ship those private configurations. Keep the old
service running until you have built a new configuration and arranged the handoff
above. Publishing or cloning this repository does not migrate a running system.

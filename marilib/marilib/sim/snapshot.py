"""Print the firmware model the simulator uses, or rewrite the bundled snapshot of it."""

import json

import click

from marilib.sim.firmware import SNAPSHOT_PATH, find_firmware_dir, parse_firmware


@click.command()
@click.option("--firmware-dir", type=click.Path(file_okay=False), default=None)
@click.option("--write", is_flag=True, help=f"Rewrite {SNAPSHOT_PATH.name}.")
def main(firmware_dir: str | None, write: bool):
    """Parse the firmware sources and print the result, or write it as the snapshot."""
    found = find_firmware_dir(firmware_dir)
    if found is None:
        raise click.ClickException("no firmware sources found; pass --firmware-dir")
    text = json.dumps(parse_firmware(found).to_json(), indent=2) + "\n"
    if write:
        SNAPSHOT_PATH.write_text(text)
        click.echo(f"wrote {SNAPSHOT_PATH} from {found}")
    else:
        click.echo(text, nl=False)


if __name__ == "__main__":
    main()

from rich.console import Console

if __name__ == "__main__":

    with Console(record=True) as console:
        console.print("This is a [bold red]red[/bold red] text.")

        console.save_svg("table.svg", title="svgfontembed")

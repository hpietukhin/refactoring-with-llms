# Reproducing the figures

`plot_data.json` contains the final CK measurements, smell-removal totals, and
aggregate refactoring trajectories used by the three data-driven figures in the
paper. Missing planner and episode combinations are absent from
`final_metrics`.

From the paper directory, regenerate the PDFs with:

```sh
uv run --with matplotlib python figures/generate_plots.py
```

Use `--data` to read another JSON file with the same schema, or `--output-dir`
to write the plots elsewhere.

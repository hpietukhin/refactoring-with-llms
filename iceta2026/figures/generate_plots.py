"""Regenerate the paper figures from the accompanying JSON data."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_PATH = SCRIPT_DIR / "plot_data.json"

COLORS = {
    "detector_order": "#0072B2",
    "daps": "#E69F00",
    "topological": "#009E73",
    "befs": "#CC79A7",
}
MARKERS = {
    "detector_order": "o",
    "daps": "s",
    "topological": "^",
    "befs": "D",
}


def load_data(path: Path) -> dict[str, Any]:
    """Load and validate the top-level plot-data fields."""
    data = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "planner_order",
        "planner_labels",
        "final_metrics",
        "fixed_smells",
        "aggregate_trajectories",
    }
    missing = required.difference(data)
    if missing:
        names = ", ".join(sorted(missing))
        raise ValueError(f"Missing plot-data fields: {names}")
    return data


def save_final_metric_figures(data: dict[str, Any], output_dir: Path) -> None:
    """Generate the final CK and size metric figures."""
    planners = data["planner_order"]
    labels = data["planner_labels"]
    rows = data["final_metrics"]
    episodes = sorted(
        {row["episode"] for row in rows},
        key=lambda episode: int(episode.removeprefix("C")),
    )
    by_key = {(row["episode"], row["planner"]): row for row in rows}

    specifications = (
        (
            "final_design_metrics.pdf",
            (("cbo", "Mean CBO"), ("lcom", "Mean LCOM"), ("wmc", "Mean WMC")),
        ),
        (
            "final_size_metrics.pdf",
            (("classes", "Classes"), ("methods", "Methods"), ("loc", "LOC")),
        ),
    )

    for filename, metrics in specifications:
        figure, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True)
        for axis, (field, ylabel) in zip(axes, metrics):
            for planner in planners:
                values = [
                    by_key.get((episode, planner), {}).get(field, math.nan)
                    for episode in episodes
                ]
                axis.plot(
                    episodes,
                    values,
                    marker=MARKERS[planner],
                    linewidth=1.2,
                    markersize=5,
                    label=labels[planner],
                )
            axis.set_ylabel(ylabel)
            axis.grid(axis="y", alpha=0.25)

        axes[0].legend(
            ncol=4,
            frameon=False,
            loc="upper center",
            bbox_to_anchor=(0.5, 1.28),
        )
        axes[-1].set_xlabel("Dataset episode")
        figure.tight_layout()
        figure.savefig(output_dir / filename, bbox_inches="tight")
        plt.close(figure)


def save_smell_figure(data: dict[str, Any], output_dir: Path) -> None:
    """Generate the fixed-smell and aggregate-trajectory figure."""
    planners = data["planner_order"]
    labels = data["planner_labels"]
    totals = data["fixed_smells"]
    trajectories = data["aggregate_trajectories"]

    figure, axes = plt.subplots(2, 2, figsize=(10, 7.2))
    axis = axes[0, 0]
    positions = list(range(len(planners)))
    original = [
        totals[planner]["original_fixed"] / totals[planner]["completed_runs"]
        for planner in planners
    ]
    introduced = [
        totals[planner]["introduced_fixed"] / totals[planner]["completed_runs"]
        for planner in planners
    ]
    axis.bar(
        positions,
        original,
        label="Original smells fixed",
        color="#4C78A8",
    )
    axis.bar(
        positions,
        introduced,
        bottom=original,
        label="Introduced smells fixed",
        color="#F58518",
    )
    for position, total in enumerate(
        first + second for first, second in zip(original, introduced)
    ):
        axis.text(
            position,
            total + 0.5,
            f"{total:.1f}",
            ha="center",
            va="bottom",
            fontsize=8,
        )
    axis.set_xticks(positions, [labels[planner] for planner in planners])
    axis.set_ylabel("Mean smells per completed run")
    axis.set_title("Fixed smells")
    axis.legend(frameon=False, fontsize=8)
    axis.grid(axis="y", alpha=0.25)

    panel_specs = (
        (
            axes[0, 1],
            "editSuccessRate",
            "Successful-edit rate",
            "Rate (%)",
        ),
        (
            axes[1, 0],
            "introducedPerFix",
            "Smells introduced per fix",
            "Introduced smells",
        ),
        (
            axes[1, 1],
            "cumulativeIntroduced",
            "Cumulative introduced smells",
            "Introduced smells",
        ),
    )
    for axis, metric, title, ylabel in panel_specs:
        records = {
            record["planner"]: record for record in trajectories[metric]
        }
        for planner in planners:
            record = records[planner]
            axis.plot(
                record["x"],
                record["y"],
                color=COLORS[planner],
                marker=MARKERS[planner],
                linewidth=1.4,
                markersize=4,
                label=f"{labels[planner]} (n={record['n']})",
            )
        axis.set_title(title)
        axis.set_xlabel("Normalized refactoring progress (%)")
        axis.set_ylabel(ylabel)
        axis.grid(alpha=0.25)
        axis.legend(frameon=False, fontsize=7)

    figure.tight_layout()
    figure.savefig(output_dir / "aggregate_smell_metrics.pdf", bbox_inches="tight")
    plt.close(figure)


def parse_args() -> argparse.Namespace:
    """Parse command-line paths."""
    parser = argparse.ArgumentParser(
        description="Regenerate the Matplotlib figures used by the paper."
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=DEFAULT_DATA_PATH,
        help="JSON input path",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=SCRIPT_DIR,
        help="Directory for generated PDF figures",
    )
    return parser.parse_args()


def main() -> None:
    """Generate every data-driven figure used by the paper."""
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    data = load_data(args.data)
    save_final_metric_figures(data, args.output_dir)
    save_smell_figure(data, args.output_dir)


if __name__ == "__main__":
    main()

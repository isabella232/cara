import base64
import dataclasses
from datetime import datetime
import io
from pathlib import Path

import jinja2
import matplotlib
matplotlib.use('agg')
import matplotlib.pyplot as plt
import numpy as np

from cara import models
from .model_generator import FormData


@dataclasses.dataclass(frozen=True)
class RepeatEvents:
    repeats: int
    probability_of_infection: float
    expected_new_cases: float


def calculate_report_data(model: models.ExposureModel):
    resolution = 600

    t_start = min(model.exposed.presence.boundaries()[0][0],
                  model.concentration_model.infected.presence.boundaries()[0][0])
    t_end = max(model.exposed.presence.boundaries()[-1][1],
                model.concentration_model.infected.presence.boundaries()[-1][1])

    times = list(np.linspace(t_start, t_end, resolution))
    concentrations = [model.concentration_model.concentration(time) for time in times]
    highest_const = max(concentrations)
    prob = model.infection_probability()
    er = model.concentration_model.infected.emission_rate_when_present()
    exposed_occupants = model.exposed.number
    expected_new_cases = model.expected_new_cases()

    repeated_events = []
    for n in [1, 2, 3, 4, 5]:
        repeat_model = dataclasses.replace(model, repeats=n)
        repeated_events.append(
            RepeatEvents(
                repeats=n,
                probability_of_infection=repeat_model.infection_probability(),
                expected_new_cases=repeat_model.expected_new_cases(),
            )
        )

    return {
        "times": times,
        "concentrations": concentrations,
        "highest_const": highest_const,
        "prob_inf": prob,
        "emission_rate": er,
        "exposed_occupants": exposed_occupants,
        "expected_new_cases": expected_new_cases,
        "scenario_plot_src": embed_figure(plot(times, concentrations, model)),
        "repeated_events": repeated_events,
    }


def embed_figure(figure) -> str:
    # Draw the scenario graph.
    img_data = io.BytesIO()

    figure.savefig(img_data, format='png', bbox_inches="tight")
    plt.close()
    img_data.seek(0)
    pic_hash = base64.b64encode(img_data.read()).decode('ascii')
    # A src suitable for a tag such as f'<img id="scenario_concentration_plot" src="{result}">.
    return f'data:image/png;base64,{pic_hash}'


def plot(times, concentrations, model: models.ExposureModel):
    fig = plt.figure()
    ax = fig.add_subplot(1, 1, 1)
    ax.plot(times, concentrations)
    ax.spines['right'].set_visible(False)
    ax.spines['top'].set_visible(False)

    ax.set_xlabel('Time (hour of day)')
    ax.set_ylabel('Concentration ($q/m^3$)')
    ax.set_title('Concentration of infectious quanta')

    # Plot overlap of exposed and infected
    overlap_start = max(model.exposed.presence.boundaries()[0][0], model.concentration_model.infected.presence.boundaries()[0][0])
    overlap_finish = min(model.exposed.presence.boundaries()[-1][1], model.concentration_model.infected.presence.boundaries()[-1][1])

    ax.plot([overlap_start, overlap_start], [0, model.concentration_model.concentration(overlap_start)], linestyle='--', color="#1f77b4")
    ax.plot([overlap_finish, overlap_finish], [0, model.concentration_model.concentration(overlap_finish)], linestyle='--', color="#1f77b4")

    plt.fill_between(times, concentrations, 0, where=(np.array(times)<overlap_start), color="white")
    plt.fill_between(times, concentrations, 0, where=(np.array(times)>=overlap_start), alpha=0.2)
    plt.fill_between(times, concentrations, 0, where=(np.array(times)>overlap_finish), color="white")

    # top = max([0.75, max(concentrations)])
    # print(max(concentrations))
    # ax.set_ylim(bottom=1e-4, top=top)
    return fig


def minutes_to_time(minutes: int) -> str:
    minute_string = str(minutes % 60)
    minute_string = "0" * (2 - len(minute_string)) + minute_string
    hour_string = str(minutes // 60)
    hour_string = "0" * (2 - len(hour_string)) + hour_string

    return f"{hour_string}:{minute_string}"


def build_report(model: models.ExposureModel, form: FormData):
    now = datetime.now()
    time = now.strftime("%d/%m/%Y %H:%M:%S")
    request = {"the": "form", "request": "data"}

    context = {
        'model': model,
        'request': request,
        'form': form,
        'creation_date': time,
    }

    context.update(calculate_report_data(model))

    cara_templates = Path(__file__).parent.parent / "templates"
    calculator_templates = Path(__file__).parent / "templates"
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader([cara_templates, calculator_templates]),
        undefined=jinja2.StrictUndefined,
    )
    env.filters['minutes_to_time'] = minutes_to_time
    env.filters['float_format'] = "{0:.2f}".format
    env.filters['int_format'] = "{:0.0f}".format
    template = env.get_template("report.html.j2")
    return template.render(**context)

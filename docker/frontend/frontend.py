import dash
from dash import dcc, html, Input, Output
import plotly.express as px
import json
import os

app = dash.Dash(__name__)

# The layout defines the structure of your page
app.layout = html.Div([
    html.H1("Investment Dashboard (Live)"),
    html.Div(id='live-update-text'),
    dcc.Graph(id='live-update-graph'),
    # Interval component: fires every 2000 milliseconds (2 seconds)
    dcc.Interval(
        id='interval-component',
        interval=2 * 1000,
        n_intervals=0
    )
])


# Callback to update the text and graph
@app.callback(
    [Output('live-update-text', 'children'),
     Output('live-update-graph', 'figure')],
    [Input('interval-component', 'n_intervals')]
)
def update_dashboard(n):
    # 1. Load the data from the JSON bridge
    try:
        with open('../cache/status.json', 'r') as f:
            data = json.load(f)
    except Exception as e:
        return f"Error reading data: {e}", px.scatter()

    # 2. Format the text display
    text_content = [
        html.P(f"Total Value: ₪{data['portfolio_value']:,}"),
        html.P(f"Unrealized PnL: ₪{data['unrealized_pnl']:,}"),
        html.Small(f"Last updated: {data['last_updated']}")
    ]

    # 3. Create a Pie Chart from holdings
    fig = px.pie(
        data['holdings'],
        values='value',
        names='symbol',
        title="Portfolio Allocation",
        hole=0.3
    )

    return text_content, fig


if __name__ == '__main__':
    # host='0.0.0.0' allows access from outside your Google VM
    app.run(debug=True, host='0.0.0.0', port=8050)
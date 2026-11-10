declare module "plotly.js-dist-min" {
  namespace Plotly {
    type Data = Record<string, unknown>;
  }
  const Plotly: {
    react(el: HTMLElement, data: Plotly.Data[], layout?: Record<string, unknown>, config?: Record<string, unknown>): Promise<unknown>;
    purge(el: HTMLElement): void;
  };
  export default Plotly;
}

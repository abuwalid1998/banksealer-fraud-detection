import {
  ArcElement, BarElement, CategoryScale, Chart as ChartJS, Filler, Legend, LinearScale, LineElement, PointElement,
  Tooltip,
} from "chart.js";

ChartJS.register(CategoryScale, LinearScale, BarElement, LineElement, PointElement, ArcElement, Tooltip, Legend, Filler);
ChartJS.defaults.font.family = "Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif";
ChartJS.defaults.font.size = 11;
ChartJS.defaults.color = "#475569";
ChartJS.defaults.plugins.legend.labels.boxWidth = 12;

export const baseOptions: any = {
  responsive: true, maintainAspectRatio: false, animation: false,
  plugins: { legend: { position: "bottom" } },
  scales: { x: { grid: { display: false } }, y: { grid: { color: "#eef2f7" }, beginAtZero: true } },
};
export const PALETTE = ["#1e3a8a", "#0ea5a4", "#e8710a", "#7c3aed", "#64748b", "#c62828"];

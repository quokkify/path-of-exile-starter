export default {
  name: "Path of Exile Starter",
  output: "./allure-report",
  plugins: {
    awesome: {
      options: {
        reportName: "Path of Exile Starter test report",
        singleFile: false,
        reportLanguage: "en",
        groupBy: ["epic", "feature", "story"],
      },
    },
  },
};

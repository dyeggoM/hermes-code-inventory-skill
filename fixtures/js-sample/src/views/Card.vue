<template>
  <div>{{ title }}</div>
</template>

<script setup>
import { ref } from "vue";
import { formatDate } from "../util/format";

const title = ref("Card");

function refresh() {
  title.value = formatDate(new Date());
}
</script>

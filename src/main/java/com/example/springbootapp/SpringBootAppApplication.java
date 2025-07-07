package com.example.springbootapp;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class SpringBootAppApplication {

	public static void main(String[] args) {
		SpringApplication.run(SpringBootAppApplication.class, args);
	}

}

// This file is correct for starting your Spring Boot application.
// If you want to fetch values from the database, ensure your ExampleService uses a repository (e.g., JpaRepository) instead of an in-memory list.
// No changes needed in this file.

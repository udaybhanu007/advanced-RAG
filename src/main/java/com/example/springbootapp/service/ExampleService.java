package com.example.springbootapp.service;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;

import com.example.springbootapp.model.ExampleModel;
import com.example.springbootapp.repository.ExampleRepository;

import java.util.List;

@Service
public class ExampleService {

    @Autowired
    private ExampleRepository exampleRepository;

    public ExampleModel saveExample(ExampleModel exampleModel) {
        return exampleRepository.save(exampleModel);
    }

    public ExampleModel getExampleById(Long id) {
        return exampleRepository.findById(id).orElse(null);
    }

    public List<ExampleModel> getAllExamples() {
        return exampleRepository.findAll();
    }

    public void deleteExample(Long id) {
        exampleRepository.deleteById(id);
    }

    public ExampleModel updateExample(Long id, ExampleModel updatedModel) {
        ExampleModel existing = exampleRepository.findById(id).orElse(null);
        if (existing != null) {
            updatedModel.setId(id);
            return exampleRepository.save(updatedModel);
        }
        return null;
    }
}
